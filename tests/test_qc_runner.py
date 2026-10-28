"""Offline fixture tests only; never read real WES FASTQ."""
import copy
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

RUNNER = Path(__file__).resolve().parents[1] / "run_qc.py"
spec = importlib.util.spec_from_file_location("run_qc", RUNNER)
qc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qc)


def fixture_reports(folder, input_path, count=2, status="warn", filename=None):
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    name = filename or Path(input_path).name
    stem = Path(input_path).name.removesuffix(".gz").removesuffix(".fq").removesuffix(".fastq")
    prefix = stem + "_fastqc"
    data = ("##FastQC\t0.12.1\n>>Basic Statistics\tpass\n#Measure\tValue\n"
            f"Filename\t{name}\nTotal Sequences\t{count}\nSequence length\t4\n"
            ">>END_MODULE\n" + f">>Per base sequence quality\t{status}\n"
            "#Base\tMean\n1\t30\n>>END_MODULE\n")
    summary = f"PASS\tBasic Statistics\t{name}\n{status.upper()}\tPer base sequence quality\t{name}\n"
    archive = folder / (prefix + ".zip")
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(prefix + "/", "")
        z.writestr(prefix + "/fastqc_data.txt", data)
        z.writestr(prefix + "/summary.txt", summary)
    report = folder / (prefix + ".html")
    report.write_text("<!doctype html><html><body>Fixture</body></html>",encoding="utf-8")
    return archive, report


class QcRunnerTests(unittest.TestCase):
    def test_repeated_stop_signal_does_not_interrupt_cleanup(self):
        import signal
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        def fake_run(args):
            handler = signal.getsignal(signal.SIGTERM)
            with self.assertRaises(KeyboardInterrupt):
                handler(signal.SIGTERM, None)
            self.assertTrue(qc.STOP.is_set())
            handler(signal.SIGTERM, None)
            return 0
        try:
            qc.STOP.clear()
            with patch.object(qc, 'run', fake_run), patch.object(qc.sys, 'argv', ['run_qc.py', '--manifest', 'dummy', '--env-prefix', 'dummy', '--out-dir', 'dummy']):
                self.assertEqual(qc.main(), 0)
        finally:
            qc.STOP.clear()
            for sig, old in previous.items():
                signal.signal(sig, old)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.tmp.name)
        self.input = self.root / "SYNTHETIC_DEMO_001_L00_R1.fq.gz"
        self.input.write_bytes(gzip.compress(b"@a/1\nACGT\n+\nIIII\n@b/1\nACGT\n+\nIIII\n",mtime=0))
        self.entry = {"sample_id":"SYNTHETIC_DEMO_001","batch":"707","lane_label":"00","mate":"R1",
                      "path":str(self.input),"size_bytes":self.input.stat().st_size,
                      "expected_md5":hashlib.md5(self.input.read_bytes()).hexdigest()}
        qc.STOP.clear()

    def tearDown(self):
        qc.STOP.clear()
        assert self.root.resolve().is_relative_to(Path(__file__).resolve().parent)
        self.tmp.cleanup()

    def test_md5_full_check_reuse_and_expected_hash_invalidation(self):
        state = qc.verify_md5(self.entry)
        self.assertEqual(state["status"],"passed")
        self.assertFalse(state["reused"])
        self.assertEqual(state["bytes_read"],self.entry["size_bytes"])
        self.assertTrue(qc.verify_md5(self.entry,state)["reused"])
        wrong = dict(self.entry,expected_md5="0"*32)
        failed = qc.verify_md5(wrong,state)
        self.assertEqual(failed["status"],"failed")
        self.assertFalse(failed["reused"])
        self.assertFalse(qc.verify_md5(self.entry,failed)["reused"])

    def test_changed_path_and_size_invalidate_cache(self):
        state=qc.verify_md5(self.entry)
        moved=self.root/"other.fq.gz"; moved.write_bytes(self.input.read_bytes())
        changed=dict(self.entry,path=str(moved))
        self.assertFalse(qc.verify_md5(changed,state)["reused"])
        self.input.write_bytes(self.input.read_bytes()+b"x")
        with self.assertRaises(qc.QcError):
            qc.verify_md5(self.entry,state)

    def test_same_size_content_and_timestamp_change_invalidates_cache(self):
        state=qc.verify_md5(self.entry)
        content=bytearray(self.input.read_bytes()); content[-3]^=1
        self.input.write_bytes(content)
        st=self.input.stat()
        os.utime(self.input,ns=(st.st_atime_ns,st.st_mtime_ns+2_000_000_000))
        new=qc.verify_md5(self.entry,state)
        self.assertEqual(new["status"],"failed")
        self.assertFalse(new["reused"])

    def test_mutation_during_hash_never_passes(self):
        def mutate(done,total):
            st=self.input.stat()
            os.utime(self.input,ns=(st.st_atime_ns,st.st_mtime_ns+2_000_000_000))
        with self.assertRaisesRegex(qc.QcError,"changed while hashing"):
            qc.verify_md5(self.entry,progress=mutate)

    def test_interrupt_checksum(self):
        qc.STOP.set()
        with self.assertRaises(InterruptedError):
            qc.verify_md5(self.entry)

    def test_zip_validation_and_warn_is_finding(self):
        z,h=fixture_reports(self.root/"valid",self.input,status="warn")
        parsed=qc.parse_fastqc(z,h,self.input)
        self.assertEqual(parsed["total_sequences"],2)
        self.assertEqual(parsed["module_status"]["Per base sequence quality"],"warn")
        z.write_bytes(b"bad zip")
        with self.assertRaises(qc.QcError):
            qc.parse_fastqc(z,h,self.input)

    def test_wrong_filename_and_truncated_module_are_rejected(self):
        z,h=fixture_reports(self.root/"wrong",self.input,filename="wrong.fq.gz")
        with self.assertRaisesRegex(qc.QcError,"filename mismatch"):
            qc.parse_fastqc(z,h,self.input)
        z,h=fixture_reports(self.root/"truncated",self.input)
        with zipfile.ZipFile(z) as archive:
            members={name:archive.read(name) for name in archive.namelist()}
        name=next(n for n in members if n.endswith("fastqc_data.txt"))
        members[name]=members[name].removesuffix(b">>END_MODULE\n")
        with zipfile.ZipFile(z,"w") as archive:
            for name,value in members.items(): archive.writestr(name,value)
        with self.assertRaisesRegex(qc.QcError,"Incomplete"):
            qc.parse_fastqc(z,h,self.input)

    def test_fastqc_cache_recipe_prefix_change_and_output_tamper(self):
        checksum=qc.verify_md5(self.entry)
        recipe={"environment":{"prefix":"/env/one","versions":{"fastqc":"0.12.1"}},"args":qc.FASTQC_ARGS}
        def fake_run(command,log_path,env,cwd=None):
            folder=Path(command[command.index("--outdir")+1])
            fixture_reports(folder,self.input)
        with patch.object(qc,"run_command",side_effect=fake_run) as command:
            first=qc.fastqc_one(self.entry,checksum,self.root/"qc",{"fastqc":Path("/fake/fastqc")},{},recipe)
            self.assertFalse(first["reused"])
            second=qc.fastqc_one(self.entry,checksum,self.root/"qc",{"fastqc":Path("/fake/fastqc")},{},recipe)
            self.assertTrue(second["reused"])
            self.assertEqual(command.call_count,1)
            changed=copy.deepcopy(recipe); changed["environment"]["prefix"]="/env/two"
            third=qc.fastqc_one(self.entry,checksum,self.root/"qc",{"fastqc":Path("/fake/fastqc")},{},changed)
            self.assertFalse(third["reused"])
            self.assertEqual(command.call_count,2)
            Path(third["html_path"]).write_text("<html>tampered</html>")
            fourth=qc.fastqc_one(self.entry,checksum,self.root/"qc",{"fastqc":Path("/fake/fastqc")},{},changed)
            self.assertFalse(fourth["reused"])
            self.assertEqual(command.call_count,3)

    def test_pair_counts(self):
        r2=dict(self.entry,mate="R2")
        results={qc.task_key(self.entry):{"total_sequences":2},qc.task_key(r2):{"total_sequences":2}}
        pair=qc.validate_pair_counts([self.entry,r2],results)
        self.assertEqual(pair[0]["read_pairs"],2)
        self.assertFalse(pair[0]["full_read_id_order_verified"])
        results[qc.task_key(r2)]["total_sequences"]=1
        with self.assertRaises(qc.QcError):
            qc.validate_pair_counts([self.entry,r2],results)

    def test_manifest_rejects_missing_mate_and_outside_root(self):
        m={"schema_version":1,"raw_root":str(self.root),"expected_samples":1,"expected_files":1,"files":[self.entry]}
        path=self.root/"manifest.json"; path.write_text(json.dumps(m))
        with self.assertRaisesRegex(qc.QcError,"pairing"):
            qc.load_manifest(path)
        m["raw_root"]=str(self.root/"isolated"); (self.root/"isolated").mkdir()
        path.write_text(json.dumps(m))
        with self.assertRaisesRegex(qc.QcError,"outside raw_root"):
            qc.load_manifest(path)

    def test_multiqc_requires_exact_names_counts_and_sources(self):
        target=self.root/"multiqc"; data=target/"multiqc_data"; data.mkdir(parents=True)
        (target/"multiqc_report.html").write_text("<html>fixture</html>")
        z,h=fixture_reports(self.root/"fastqc",self.input)
        metrics=qc.parse_fastqc(z,h,self.input)
        result={"metrics":metrics,"zip_path":str(z),"html_path":str(h)}
        raw={self.input.name:{"Total Sequences":2.0}}
        sources={"FastQC":{"all_sections":{self.input.name:str(z)}}}
        (data/"multiqc_fastqc.json").write_text(json.dumps(raw))
        (data/"multiqc_sources.json").write_text(json.dumps(sources))
        self.assertEqual(qc.validate_multiqc(target,[result])["validated_files"],1)
        raw[self.input.name]["Total Sequences"]=3
        (data/"multiqc_fastqc.json").write_text(json.dumps(raw))
        with self.assertRaisesRegex(qc.QcError,"read count"):
            qc.validate_multiqc(target,[result])
        raw[self.input.name]["Total Sequences"]=2
        (data/"multiqc_fastqc.json").write_text(json.dumps(raw))
        sources["FastQC"]["all_sections"][self.input.name]=str(self.root/"stale.zip")
        (data/"multiqc_sources.json").write_text(json.dumps(sources))
        with self.assertRaisesRegex(qc.QcError,"source ZIP set"):
            qc.validate_multiqc(target,[result])
        (data/"multiqc_sources.json").write_text(json.dumps({"FastQC":{"all_sections":{self.input.name:str(z)}}}))
        raw["extra.fq.gz"]={"Total Sequences":2}
        (data/"multiqc_fastqc.json").write_text(json.dumps(raw))
        with self.assertRaisesRegex(qc.QcError,"accounting"):
            qc.validate_multiqc(target,[result])


if __name__=="__main__":
    unittest.main(verbosity=2)

