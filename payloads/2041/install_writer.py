
import base64, datetime, hashlib, json, os, pathlib, py_compile, shutil, subprocess, sys, tempfile

EXPECTED = "ee7e693a303e0bae31aa1ebe1e965199e961b75d31d8209a94fe6a5119e1ed22"
CANDIDATE_INPUT = pathlib.Path(sys.argv[1]).resolve()
if not CANDIDATE_INPUT.is_file():
    raise SystemExit("candidate input missing")
SOURCE = CANDIDATE_INPUT.read_bytes()
EXPECTED_NEW = "f69c0da42ad0265c448976deb60ace7bccea0d22da77557ba776ee5886ef42d2"
TARGET = pathlib.Path("/home/jose/moreno-ai/runtime/moreno_chatgpt_inbox_writer.py")
REPO = pathlib.Path("/home/jose/moreno-ai")
AUDIT_DIR = REPO / "memory/audit/candidates/repair-claude-gmail-relay-transport-20260702-001"
ROLLBACK_DIR = REPO / "memory/audit/rollback"
TS = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()

def sha_file(path):
    return sha_bytes(path.read_bytes())

def atomic_bytes(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="."+path.name+".", dir=str(path.parent))
    temp = pathlib.Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        dfd = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        try: temp.unlink()
        except FileNotFoundError: pass

def run_candidate(candidate):
    tests = []
    def call(payload, base, args=None):
        env = os.environ.copy()
        env["MORENO_CHATGPT_INBOX_BASE"] = str(base)
        return subprocess.run(
            [sys.executable, str(candidate)] + (args or []),
            input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=20
        )
    root = pathlib.Path(tempfile.mkdtemp(prefix="moreno-writer-test-"))
    try:
        entry = {"messageId":"m1","threadId":"t1","subject":"[MORENO CLAUDE] test",
                 "requestId":"r1","status":"STARTED","taskId":"task","participant":"claude","body":"hello"}
        r1=call({"entries":[entry]},root/"a"); o1=json.loads(r1.stdout)
        assert r1.returncode==0 and o1["added_count"]==1 and o1["event_keys_added"]==["r1:STARTED"]
        tests.append("first_write")
        r2=call({"entries":[entry]},root/"a"); o2=json.loads(r2.stdout)
        assert r2.returncode==0 and o2["added_count"]==0 and o2["duplicate_existing_count"]==1
        tests.append("restart_dedup")
        complete={**entry,"messageId":"m2","status":"COMPLETED","body":"done"}
        r3=call({"entries":[complete]},root/"a"); o3=json.loads(r3.stdout)
        inbox=json.loads((root/"a/chatgpt_inbox.json").read_text())
        assert r3.returncode==0 and o3["added_count"]==1 and [x["event_key"] for x in inbox]==["r1:STARTED","r1:COMPLETED"]
        tests.append("status_separation")
        r4=call({"entries":[entry,entry]},root/"b"); o4=json.loads(r4.stdout)
        assert r4.returncode==0 and o4["added_count"]==1 and o4["duplicate_batch_count"]==1
        tests.append("same_batch_dedup")
        bad={k:v for k,v in entry.items() if k!="messageId"}
        r5=call({"entries":[bad]},root/"c")
        assert r5.returncode!=0 and (root/"c/relay_failures.json").is_file() and not (root/"c/chatgpt_inbox.json").exists()
        tests.append("missing_id_fails_durable")
        corrupt=root/"d"; corrupt.mkdir(); cp=corrupt/"chatgpt_inbox.json"; cp.write_text("{broken")
        before=cp.read_bytes(); r6=call({"entries":[entry]},corrupt)
        assert r6.returncode!=0 and cp.read_bytes()==before and (corrupt/"relay_failures.json").is_file()
        tests.append("corrupt_state_preserved")
        r7=call({"category":"MISSING_MESSAGE_ID","detail":"controlled"},root/"e",["--record-error"])
        assert r7.returncode==0 and json.loads(r7.stdout)["recorded"] is True
        tests.append("durable_error_mode")
        return tests
    finally:
        shutil.rmtree(root, ignore_errors=True)

if not TARGET.is_file():
    raise SystemExit("target writer missing")
before_sha = sha_file(TARGET)
if before_sha != EXPECTED:
    raise SystemExit("writer SHA guard mismatch: "+before_sha)
if sha_bytes(SOURCE) != EXPECTED_NEW:
    raise SystemExit("candidate SHA mismatch")
text = SOURCE.decode("utf-8")
for forbidden in ("sshpass -p", "StrictHostKeyChecking=no", "BEGIN PRIVATE KEY", "ghp_"):
    if forbidden in text:
        raise SystemExit("forbidden secret/transport pattern in candidate: "+forbidden)

AUDIT_DIR.mkdir(parents=True, exist_ok=True)
ROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
candidate = AUDIT_DIR / ("moreno_chatgpt_inbox_writer.py.candidate-"+TS)
atomic_bytes(candidate, SOURCE, 0o700)
py_compile.compile(str(candidate), doraise=True)
tests = run_candidate(candidate)

rollback = ROLLBACK_DIR / ("moreno_chatgpt_inbox_writer.py."+TS+".bak")
atomic_bytes(rollback, TARGET.read_bytes(), 0o600)
atomic_bytes(TARGET, SOURCE, 0o755)
py_compile.compile(str(TARGET), doraise=True)
after_sha = sha_file(TARGET)
if after_sha != EXPECTED_NEW:
    atomic_bytes(TARGET, rollback.read_bytes(), 0o755)
    raise SystemExit("read-back SHA mismatch; rollback restored")

record = {
    "task_id":"repair-claude-gmail-relay-transport-20260702-001",
    "component":"runtime/moreno_chatgpt_inbox_writer.py",
    "installed_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "before_sha256":before_sha,
    "after_sha256":after_sha,
    "rollback_path":str(rollback),
    "candidate_path":str(candidate),
    "tests":tests,
    "secret_scan":"passed",
    "live_inbox_touched":False,
}
audit = AUDIT_DIR / ("writer-install-"+TS+".json")
atomic_bytes(audit, (json.dumps(record,indent=2,sort_keys=True)+"\n").encode(), 0o600)
print(json.dumps({"ok":True,"before_sha256":before_sha,"after_sha256":after_sha,
                  "rollback_path":str(rollback),"audit_path":str(audit),"tests":tests},separators=(",",":")))
