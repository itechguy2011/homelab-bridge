#!/usr/bin/env python3
"""Bounded repair utility for the Moreno ChatGPT Gmail poller.

This script manages only workflow ``0MUKgyQwZAV1xLTk`` and only the exact
``moreno-chatgpt-email-poller`` workflow name. It uses pinned, noninteractive
SSH to the n8n host, where the n8n credential is read locally and never
returned. The script supports dry-run, apply, and rollback modes.

The apply transaction is fail closed:
1. run isolated writer and patch self-tests;
2. fetch and validate the live workflow;
3. create a rollback export and SHA-256;
4. build and validate the patched workflow without mutation;
5. PUT the bounded update;
6. read back and verify the expected graph and safeguards;
7. automatically restore the rollback export if post-PUT verification fails.
"""
from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

REPO = Path("/home/jose/moreno-ai")
WORKFLOW_ID = "0MUKgyQwZAV1xLTk"
WORKFLOW_NAME = "moreno-chatgpt-email-poller"
N8N_HOST = "192.168.0.191"
N8N_USER = "jose"
REMOTE_CREDENTIAL_FILE = "/home/jose/.secrets/n8n_api_key"
WRITER_PATH = REPO / "runtime/moreno_chatgpt_inbox_writer.py"
AUDIT_ROOT = REPO / "memory/audit/candidates/repair-claude-gmail-relay-transport-20260702-001"
ROLLBACK_ROOT = REPO / "memory/audit/rollback"
EXPECTED_GMAIL_CREDENTIAL = "wB7CoG4QJdO1FsPU"
EXPECTED_SSH_CREDENTIAL = "RIzubZQgcErsLYJG"
SSH_IDENTITIES = (
    Path("/home/jose/.ssh/id_moreno"),
    Path("/home/jose/.ssh/n8n_bridge"),
)

EXCLUDED_PUT_FIELDS = {
    "updatedAt", "createdAt", "id", "activeVersion", "activeVersionId",
    "triggerCount", "versionCounter", "isArchived", "shared",
    "sourceWorkflowId", "nodeGroups", "description", "meta",
    "active", "versionId", "staticData", "tags",
}
SETTINGS_EXCLUDE = {"availableInMCP", "binaryMode"}

MIME_CODE = r"""
function decodeUrlSafe(value) {
  if (!value) return '';
  let text = String(value).replace(/-/g, '+').replace(/_/g, '/');
  while (text.length % 4) text += '=';
  try { return Buffer.from(text, 'base64').toString('utf8'); } catch (_) { return ''; }
}
function stripHtml(value) {
  return String(value || '')
    .replace(/<br\s*\/?>/gi, '\n').replace(/<\/p>/gi, '\n')
    .replace(/<[^>]+>/g, ' ').replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&').replace(/&lt;/gi, '<').replace(/&gt;/gi, '>')
    .replace(/\r/g, '').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
}
function collectParts(part, plain, html) {
  if (!part || typeof part !== 'object') return;
  const mime = String(part.mimeType || '').toLowerCase();
  const data = part.body && part.body.data ? decodeUrlSafe(part.body.data) : '';
  if (data && mime === 'text/plain') plain.push(data);
  else if (data && mime === 'text/html') html.push(data);
  for (const child of (part.parts || [])) collectParts(child, plain, html);
}
const output = [];
for (const item of $input.all()) {
  const source = item.json || {};
  const plain = [], html = [];
  collectParts(source.payload, plain, html);
  let body = plain.join('\n').trim();
  if (!body && html.length) body = stripHtml(html.join('\n'));
  if (!body) body = String(source.textPlain ?? source.text ?? source.bodyText ?? source.body ?? source.snippet ?? '').trim();
  const messageId = String(source.messageId ?? source.gmail_message_id ?? source.id ?? '').trim();
  const threadId = String(source.threadId ?? source.thread_id ?? '').trim();
  output.push({json:{...source,messageId,gmail_message_id:messageId,threadId,thread_id:threadId,body,cleanBody:body}});
}
return output;
""".strip()

METADATA_CODE = r"""
function parseMetadata(body) {
  const text = String(body || '').replace(/\r/g, '');
  const markers = ['--- END RELAY METADATA ---','--- END MORENO METADATA ---','--- END METADATA ---'];
  let metadataText = text, content = '', found = false;
  for (const marker of markers) {
    const index = text.indexOf(marker);
    if (index >= 0) { metadataText=text.slice(0,index); content=text.slice(index+marker.length).trim(); found=true; break; }
  }
  if (!found) {
    const legacy=text.match(/(?:^|\n)\s*---+\s*(?:message|body|content)\s*---+\s*\n/i);
    if (legacy && legacy.index !== undefined) {metadataText=text.slice(0,legacy.index);content=text.slice(legacy.index+legacy[0].length).trim();found=true;}
  }
  const metadata={};
  for (const line of metadataText.split('\n')) {
    const match=line.match(/^\s*([A-Za-z][A-Za-z0-9_.-]*)\s*[:=]\s*(.*?)\s*$/);
    if (match) metadata[match[1].toLowerCase()]=match[2];
  }
  return {metadata,content:content||text.trim(),format:found?'header_block':'legacy_scan'};
}
const output=[];
for (const item of $input.all()) {
  const source=item.json||{}, subject=String(source.subject??source.Subject??'').trim();
  const body=String(source.cleanBody??source.body??source.text??'').trim(), parsed=parseMetadata(body), metadata=parsed.metadata;
  const requestMatch=body.match(/\brequest[_-]?id\s*[:=]\s*([A-Za-z0-9][A-Za-z0-9._-]{0,199})/i);
  const statusMatch=subject.match(/\]\s*(STARTED|COMPLETED|FAILED|BLOCKED|STATUS)\b/i);
  const messageId=String(source.messageId??source.gmail_message_id??source.id??'').trim();
  const requestId=String(metadata.request_id??metadata.requestid??(requestMatch?requestMatch[1]:'')).trim();
  const status=String(metadata.status??(statusMatch?statusMatch[1]:'')).trim().toUpperCase();
  const participant=String(metadata.participant??'claude').trim().toLowerCase();
  const taskId=String(metadata.task_id??metadata.taskid??'').trim();
  const subjectValid=subject.startsWith('[MORENO CLAUDE]')||subject.startsWith('[MORENO CLAUDE STATUS]');
  let errorCategory='';
  if (!messageId) errorCategory='MISSING_MESSAGE_ID';
  else if (!subjectValid) errorCategory='INVALID_SUBJECT';
  else if (!requestId) errorCategory='MISSING_REQUEST_ID';
  else if (!status) errorCategory='MISSING_STATUS';
  else if (participant!=='claude') errorCategory='INVALID_PARTICIPANT';
  const valid=!errorCategory;
  output.push({json:{...source,messageId,gmail_message_id:messageId,requestId,request_id:requestId,status,participant,taskId,task_id:taskId,subject,body:parsed.content,metadataFormat:parsed.format,_valid:valid,_errorCategory:errorCategory,_errorDetail:valid?'':`Claude response validation failed: ${errorCategory}`}});
}
return output;
""".strip()

COLLECT_CODE = r"""
const entries=[];
for (const item of $input.all()) {
  const value=item.json||{};
  if (!value._valid||!value.messageId) continue;
  entries.push({messageId:value.messageId,threadId:value.threadId||value.thread_id||'',subject:value.subject||'',requestId:value.requestId||value.request_id||'',status:value.status||'',taskId:value.taskId||value.task_id||'',participant:value.participant||'claude',body:value.body||value.cleanBody||'',polledAt:new Date().toISOString()});
}
if (!entries.length) return [];
const payload=JSON.stringify({entries});
return [{json:{entries_b64:Buffer.from(payload,'utf8').toString('base64'),messageIds:entries.map(e=>e.messageId),eventKeys:entries.map(e=>`${e.requestId}:${String(e.status).toUpperCase()}`),count:entries.length}}];
""".strip()

VERIFY_CODE = r"""
const output=[];
for (const item of $input.all()) {
  const value=item.json||{}, stderr=String(value.stderr??'').trim(), exitCode=Number(value.exitCode??value.code??value.returnCode??0);
  if (stderr) throw new Error(`Inbox writer stderr: ${stderr.slice(0,1000)}`);
  if (Number.isFinite(exitCode)&&exitCode!==0) throw new Error(`Inbox writer exit code ${exitCode}`);
  const lines=String(value.stdout??'').trim().split(/\r?\n/).filter(Boolean);
  if (!lines.length) throw new Error('Inbox writer returned no JSON result');
  let result; try {result=JSON.parse(lines[lines.length-1]);} catch(error){throw new Error(`Inbox writer returned invalid JSON: ${error.message}`);}
  if (result.ok!==true) throw new Error(`Inbox writer did not confirm success: ${JSON.stringify(result)}`);
  if (!/^[a-f0-9]{64}$/.test(String(result.inbox_sha256||''))) throw new Error('Inbox writer did not return a valid inbox SHA-256');
  const ids=Array.isArray(result.message_ids_received)?result.message_ids_received:[];
  if (!ids.length||ids.some(id=>!String(id||'').trim())) throw new Error('Inbox writer result is missing received Gmail message IDs');
  output.push({json:{writerResult:result,messageIds:ids}});
}
return output;
""".strip()

PREPARE_PROCESSED_CODE = r"""
const output=[];
for (const item of $input.all()) {
  const value=item.json||{};
  for (const rawId of (value.messageIds||[])) {
    const messageId=String(rawId||'').trim(); if (!messageId) continue;
    output.push({json:{messageId,inbox_sha256:value.writerResult&&value.writerResult.inbox_sha256,event_keys_added:value.writerResult&&value.writerResult.event_keys_added}});
  }
}
return output;
""".strip()

PREPARE_ERROR_CODE = r"""
const output=[];
for (const item of $input.all()) {
  const value=item.json||{}; if (String(value.messageId||'').trim()) continue;
  const record={category:value._errorCategory||'MISSING_MESSAGE_ID',request_id:value.requestId||value.request_id||'',status:value.status||'',message_id:'',workflow:'moreno-chatgpt-email-poller',detail:value._errorDetail||'Invalid Claude response without a Gmail message ID'};
  output.push({json:{error_b64:Buffer.from(JSON.stringify(record),'utf8').toString('base64'),category:record.category}});
}
return output;
""".strip()

WRITER_COMMAND = r"""set -Eeuo pipefail
printf '%s' '={{ $json.entries_b64 }}' | base64 --decode | ssh -i /home/jose/.ssh/id_moreno -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/home/jose/.ssh/known_hosts -o ConnectTimeout=10 jose@192.168.0.205 'python3 /home/jose/moreno-ai/runtime/moreno_chatgpt_inbox_writer.py'
""".strip()
ERROR_COMMAND = r"""set -Eeuo pipefail
printf '%s' '={{ $json.error_b64 }}' | base64 --decode | ssh -i /home/jose/.ssh/id_moreno -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/home/jose/.ssh/known_hosts -o ConnectTimeout=10 jose@192.168.0.205 'python3 /home/jose/moreno-ai/runtime/moreno_chatgpt_inbox_writer.py --record-error'
""".strip()

class RepairError(RuntimeError): pass

def utc_stamp(): return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
def sha_bytes(data): return hashlib.sha256(data).hexdigest()
def sha_file(path): return sha_bytes(path.read_bytes())

def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True); fd,name=tempfile.mkstemp(prefix=f".{path.name}.",dir=str(path.parent)); temporary=Path(name)
    try:
        with os.fdopen(fd,"wb") as handle: handle.write(data);handle.flush();os.fsync(handle.fileno())
        os.chmod(temporary,mode);os.replace(temporary,path);directory_fd=os.open(path.parent,os.O_DIRECTORY)
        try:os.fsync(directory_fd)
        finally:os.close(directory_fd)
    finally:
        try:temporary.unlink()
        except FileNotFoundError:pass

def atomic_json(path,value): atomic_write(path,(json.dumps(value,indent=2,ensure_ascii=False,sort_keys=True)+"\n").encode())

def choose_identity():
    available=[path for path in SSH_IDENTITIES if path.is_file() and os.access(path,os.R_OK)]
    if not available: raise RepairError("No approved pinned SSH identity is readable")
    return available[0]

def ssh_base():
    return ["ssh","-i",str(choose_identity()),"-o","IdentitiesOnly=yes","-o","BatchMode=yes","-o","StrictHostKeyChecking=yes","-o","ConnectTimeout=10",f"{N8N_USER}@{N8N_HOST}"]

def remote_api(method,path,body=None):
    if method not in {"GET","PUT"}:raise RepairError(f"Unsupported remote method: {method}")
    if not path.startswith("/api/v1/"):raise RepairError("Remote path must remain inside /api/v1/")
    encoded=base64.b64encode(json.dumps({"method":method,"path":path,"body":body},separators=(",",":")).encode()).decode()
    remote_script=f"""
import base64,json,pathlib,urllib.error,urllib.request
record=json.loads(base64.b64decode({encoded!r}))
key=pathlib.Path({REMOTE_CREDENTIAL_FILE!r}).read_text(encoding='utf-8').strip()
if not key:raise SystemExit('n8n credential file is empty')
url='http://127.0.0.1:5678'+record['path'];data=json.dumps(record['body']).encode() if record.get('body') is not None else None
request=urllib.request.Request(url,data=data,method=record['method'],headers={{'X-N8N-API-KEY':key,'Content-Type':'application/json','Accept':'application/json'}})
try:
 with urllib.request.urlopen(request,timeout=45) as response:print(response.read().decode())
except urllib.error.HTTPError as exc:
 detail=exc.read().decode(errors='replace');raise SystemExit('n8n HTTP '+str(exc.code)+': '+detail[:2000])
"""
    result=subprocess.run([*ssh_base(),"python3","-"],input=remote_script,text=True,capture_output=True,timeout=90)
    if result.returncode!=0:raise RepairError(f"Remote n8n request failed: {result.stderr[-2000:] or result.stdout[-2000:]}")
    if result.stderr.strip():raise RepairError(f"Remote n8n request emitted stderr: {result.stderr[-2000:]}")
    try:value=json.loads(result.stdout)
    except Exception as exc:raise RepairError(f"Remote n8n response was not JSON: {result.stdout[-2000:]}") from exc
    if not isinstance(value,dict):raise RepairError("Remote n8n response was not an object")
    return value

def prepare_put_body(workflow):
    body={key:value for key,value in workflow.items() if key not in EXCLUDED_PUT_FIELDS}
    if isinstance(body.get("settings"),dict):body["settings"]={key:value for key,value in body["settings"].items() if key not in SETTINGS_EXCLUDE}
    return body

def canonical_hash(workflow):
    def normalized(value):
        if isinstance(value,dict):return {key:normalized(item) for key,item in sorted(value.items()) if key not in EXCLUDED_PUT_FIELDS}
        if isinstance(value,list):return [normalized(item) for item in value]
        return value
    return sha_bytes(json.dumps(normalized(workflow),separators=(",",":"),ensure_ascii=False).encode())

def node_index(workflow):
    result={}
    for node in workflow.get("nodes",[]):
        name=str(node.get("name") or "")
        if not name or name in result:raise RepairError(f"Workflow contains a missing or duplicate node name: {name!r}")
        result[name]=node
    return result

def find_node(workflow,exact_names,required_words=()):
    index=node_index(workflow)
    for name in exact_names:
        if name in index:return index[name]
    matches=[node for name,node in index.items() if required_words and all(word.lower() in name.lower() for word in required_words)]
    if len(matches)!=1:raise RepairError(f"Expected one node for {exact_names or required_words}, found {[node.get('name') for node in matches]}")
    return matches[0]

def set_code(node,code):
    if node.get("type")!="n8n-nodes-base.code":raise RepairError(f"Expected Code node: {node.get('name')}")
    node["typeVersion"]=max(float(node.get("typeVersion") or 2),2);node["parameters"]={"mode":"runOnceForAllItems","jsCode":code}

def boolean_true_parameters(field):
    return {"conditions":{"options":{"caseSensitive":True,"leftValue":"","typeValidation":"strict","version":2},"conditions":[{"id":str(uuid.uuid5(uuid.NAMESPACE_URL,f"{WORKFLOW_ID}:{field}:true")),"leftValue":f"={{ $json.{field} }}","rightValue":"","operator":{"type":"boolean","operation":"true","singleValue":True}}],"combinator":"and"},"options":{}}

def connection(target):return {"node":target,"type":"main","index":0}
def set_main_connections(workflow,source,branches):workflow.setdefault("connections",{})[source]={"main":branches}
def make_node(name,node_type,parameters,position,credentials=None,type_version=2):
    node={"parameters":parameters,"id":str(uuid.uuid5(uuid.NAMESPACE_URL,f"{WORKFLOW_ID}:{name}")),"name":name,"type":node_type,"typeVersion":type_version,"position":position}
    if credentials:node["credentials"]=copy.deepcopy(credentials)
    return node

def patch_workflow(original):
    if original.get("id")!=WORKFLOW_ID:raise RepairError("Workflow ID mismatch")
    if original.get("name")!=WORKFLOW_NAME:raise RepairError(f"Workflow name mismatch: {original.get('name')!r}")
    workflow=copy.deepcopy(original)
    mime=find_node(workflow,("Extract MIME Body",),("Extract","MIME"));metadata=find_node(workflow,("Extract Claude Response Metadata",),("Extract","Claude","Metadata"));valid_if=find_node(workflow,("Claude Response Valid",),("Claude","Response","Valid"));any_new=find_node(workflow,("Any New Messages?",),("Any","New","Messages"));collect=find_node(workflow,("Collect All Messages",),("Collect","Messages"));writer=find_node(workflow,("Write Inbox to vm-memory",),("Write","Inbox"));prepare_processed=find_node(workflow,("Prepare Label Processed","Prepare Processed Labels"),("Prepare","Processed"));processed=find_node(workflow,("Label Processed Claude Draft","Label Processed"),("Label","Processed"));failed=find_node(workflow,("Label Failed Claude Draft",),("Label","Failed","Claude"))
    set_code(mime,MIME_CODE);set_code(metadata,METADATA_CODE);set_code(collect,COLLECT_CODE);set_code(prepare_processed,PREPARE_PROCESSED_CODE)
    if valid_if.get("type")!="n8n-nodes-base.if" or any_new.get("type")!="n8n-nodes-base.if":raise RepairError("Expected IF nodes are missing")
    valid_if["parameters"]=boolean_true_parameters("_valid");any_new["parameters"]=boolean_true_parameters("_valid")
    if writer.get("type")!="n8n-nodes-base.ssh":raise RepairError("Writer must remain an SSH node")
    writer["parameters"]={"authentication":"privateKey","command":WRITER_COMMAND};writer["continueOnFail"]=False;writer["onError"]="stopWorkflow"
    writer_credentials=copy.deepcopy(writer.get("credentials") or {});credential_record=writer_credentials.get("sshPrivateKey")
    if not isinstance(credential_record,dict) or credential_record.get("id")!=EXPECTED_SSH_CREDENTIAL:raise RepairError("Writer SSH credential reference changed or is unexpected")
    processed.setdefault("parameters",{})["messageId"]="={{ $json.messageId }}";failed.setdefault("parameters",{})["messageId"]="={{ $json.messageId }}"
    names=("Verify Inbox Write","Invalid Has Message ID?","Prepare Durable Poller Error","Record Durable Poller Error");existing=node_index(workflow)
    for name in names:
        if name in existing:workflow["nodes"]=[node for node in workflow["nodes"] if node.get("name")!=name];workflow.get("connections",{}).pop(name,None)
    writer_position=list(writer.get("position") or [1200,400]);failed_position=list(failed.get("position") or [1000,700])
    verify=make_node(names[0],"n8n-nodes-base.code",{"mode":"runOnceForAllItems","jsCode":VERIFY_CODE},[writer_position[0]+240,writer_position[1]])
    invalid_guard=make_node(names[1],"n8n-nodes-base.if",boolean_true_parameters("messageId"),[failed_position[0]-240,failed_position[1]],type_version=2.2)
    prepare_error=make_node(names[2],"n8n-nodes-base.code",{"mode":"runOnceForAllItems","jsCode":PREPARE_ERROR_CODE},[failed_position[0],failed_position[1]+220])
    record_error=make_node(names[3],"n8n-nodes-base.ssh",{"authentication":"privateKey","command":ERROR_COMMAND},[failed_position[0]+260,failed_position[1]+220],writer_credentials,float(writer.get("typeVersion") or 1));record_error["continueOnFail"]=False;record_error["onError"]="stopWorkflow"
    workflow["nodes"].extend([verify,invalid_guard,prepare_error,record_error])
    set_main_connections(workflow,valid_if["name"],[[connection(any_new["name"])],[connection(names[1])]]);set_main_connections(workflow,any_new["name"],[[connection(collect["name"])],[]]);set_main_connections(workflow,collect["name"],[[connection(writer["name"])]]);set_main_connections(workflow,writer["name"],[[connection(names[0])]]);set_main_connections(workflow,names[0],[[connection(prepare_processed["name"])]]);set_main_connections(workflow,prepare_processed["name"],[[connection(processed["name"])]]);set_main_connections(workflow,names[1],[[connection(failed["name"])],[connection(names[2])]]);set_main_connections(workflow,names[2],[[connection(names[3])]]);set_main_connections(workflow,names[3],[[]])
    validate_patched_workflow(workflow);return workflow

def validate_patched_workflow(workflow):
    if workflow.get("id")!=WORKFLOW_ID or workflow.get("name")!=WORKFLOW_NAME:raise RepairError("Patched workflow identity mismatch")
    index=node_index(workflow);required={"Extract MIME Body","Extract Claude Response Metadata","Claude Response Valid","Any New Messages?","Collect All Messages","Write Inbox to vm-memory","Verify Inbox Write","Invalid Has Message ID?","Prepare Durable Poller Error","Record Durable Poller Error"};missing=sorted(required-set(index))
    if missing:raise RepairError(f"Patched workflow is missing nodes: {missing}")
    serialized=json.dumps(workflow,ensure_ascii=False)
    for forbidden in ("sshpass -p","StrictHostKeyChecking=no","BEGIN PRIVATE KEY"):
        if forbidden in serialized:raise RepairError(f"Forbidden transport or secret pattern remains: {forbidden}")
    if "$input.first()" in index["Extract MIME Body"]["parameters"]["jsCode"] or "$input.first()" in index["Extract Claude Response Metadata"]["parameters"]["jsCode"]:raise RepairError("First-item-only processing remains")
    command=index["Write Inbox to vm-memory"]["parameters"].get("command","")
    for required_text in ("base64 --decode","BatchMode=yes","IdentitiesOnly=yes","StrictHostKeyChecking=yes","moreno_chatgpt_inbox_writer.py"):
        if required_text not in command:raise RepairError(f"Writer command missing safeguard: {required_text}")
    connections=workflow.get("connections") or {};writer_next=connections.get("Write Inbox to vm-memory",{}).get("main",[])
    if not writer_next or not writer_next[0] or writer_next[0][0].get("node")!="Verify Inbox Write":raise RepairError("Writer output does not flow through verification")
    invalid=connections.get("Invalid Has Message ID?",{}).get("main",[])
    if len(invalid)<2 or not invalid[1] or invalid[1][0].get("node")!="Prepare Durable Poller Error":raise RepairError("Missing-ID branch is not durable")
    for node in workflow.get("nodes",[]):
        if node.get("type") in {"n8n-nodes-base.gmail","n8n-nodes-base.gmailTrigger"}:
            reference=(node.get("credentials") or {}).get("gmailOAuth2")
            if isinstance(reference,dict) and reference.get("id")!=EXPECTED_GMAIL_CREDENTIAL:raise RepairError(f"Unexpected Gmail credential on {node.get('name')}")

def patch_self_test():
    def basic(name,node_type,position,credentials=None):
        node={"id":str(uuid.uuid4()),"name":name,"type":node_type,"typeVersion":2,"position":position,"parameters":{"jsCode":"return [$input.first()];"} if node_type=="n8n-nodes-base.code" else {}}
        if credentials:node["credentials"]=credentials
        return node
    gmail={"gmailOAuth2":{"id":EXPECTED_GMAIL_CREDENTIAL,"name":"Moreno Gmail OAuth"}};ssh={"sshPrivateKey":{"id":EXPECTED_SSH_CREDENTIAL,"name":"SSH Private Key account"}}
    nodes=[basic("Extract MIME Body","n8n-nodes-base.code",[0,0]),basic("Extract Claude Response Metadata","n8n-nodes-base.code",[200,0]),basic("Claude Response Valid","n8n-nodes-base.if",[400,0]),basic("Any New Messages?","n8n-nodes-base.if",[600,0]),basic("Collect All Messages","n8n-nodes-base.code",[800,0]),basic("Write Inbox to vm-memory","n8n-nodes-base.ssh",[1000,0],ssh),basic("Prepare Label Processed","n8n-nodes-base.code",[1200,0]),basic("Label Processed Claude Draft","n8n-nodes-base.gmail",[1400,0],gmail),basic("Label Failed Claude Draft","n8n-nodes-base.gmail",[800,400],gmail)]
    patched=patch_workflow({"id":WORKFLOW_ID,"name":WORKFLOW_NAME,"active":True,"nodes":nodes,"connections":{},"settings":{"executionOrder":"v1"}});validate_patched_workflow(patched);return {"ok":True,"node_count":len(patched["nodes"])}

def run_writer_self_test():
    if not WRITER_PATH.is_file():raise RepairError(f"Writer is missing: {WRITER_PATH}")
    result=subprocess.run([sys.executable,"-S",str(WRITER_PATH),"--self-test"],text=True,capture_output=True,timeout=120)
    if result.returncode!=0:raise RepairError(f"Writer self-test failed: {result.stderr[-2000:] or result.stdout[-2000:]}")
    if result.stderr.strip():raise RepairError(f"Writer self-test emitted stderr: {result.stderr[-2000:]}")
    try:value=json.loads(result.stdout)
    except Exception as exc:raise RepairError("Writer self-test did not return JSON") from exc
    if value.get("ok") is not True:raise RepairError(f"Writer self-test did not pass: {value}")
    return value

def fetch_live():
    workflow=remote_api("GET",f"/api/v1/workflows/{WORKFLOW_ID}")
    if workflow.get("id")!=WORKFLOW_ID or workflow.get("name")!=WORKFLOW_NAME:raise RepairError("Live workflow identity mismatch")
    return workflow

def put_workflow(workflow):return remote_api("PUT",f"/api/v1/workflows/{WORKFLOW_ID}",prepare_put_body(workflow))
def write_audit(record):AUDIT_ROOT.mkdir(parents=True,exist_ok=True);path=AUDIT_ROOT/f"poller-repair-{utc_stamp()}.json";atomic_json(path,record);return path

def dry_run():
    writer_tests=run_writer_self_test();patch_tests=patch_self_test();current=fetch_live();patched=patch_workflow(current);AUDIT_ROOT.mkdir(parents=True,exist_ok=True);candidate_path=AUDIT_ROOT/f"poller-patched-dry-run-{utc_stamp()}.json";atomic_json(candidate_path,patched);result={"ok":True,"mode":"dry_run","workflow_id":WORKFLOW_ID,"workflow_name":WORKFLOW_NAME,"before_sha256":canonical_hash(current),"candidate_sha256":canonical_hash(patched),"candidate_path":str(candidate_path),"writer_self_test":writer_tests,"patch_self_test":patch_tests,"live_mutation":False};audit=write_audit(result);result["audit_path"]=str(audit);return result

def apply_repair():
    writer_tests=run_writer_self_test();patch_tests=patch_self_test();current=fetch_live();before_sha=canonical_hash(current);patched=patch_workflow(current);candidate_sha=canonical_hash(patched);ROLLBACK_ROOT.mkdir(parents=True,exist_ok=True);stamp=utc_stamp();rollback_path=ROLLBACK_ROOT/f"chatgpt-email-poller-{WORKFLOW_ID}.{stamp}.json";atomic_json(rollback_path,current);rollback_sha=sha_file(rollback_path);dry_run_path=AUDIT_ROOT/f"poller-patched-before-put-{stamp}.json";atomic_json(dry_run_path,patched);put_started=False
    try:
        put_workflow(patched);put_started=True;readback=fetch_live();validate_patched_workflow(readback);after_sha=canonical_hash(readback)
        if after_sha!=candidate_sha:raise RepairError(f"Read-back canonical SHA mismatch: expected {candidate_sha}, got {after_sha}")
    except Exception:
        if put_started:put_workflow(current)
        raise
    result={"ok":True,"mode":"apply","workflow_id":WORKFLOW_ID,"workflow_name":WORKFLOW_NAME,"before_sha256":before_sha,"after_sha256":after_sha,"rollback_path":str(rollback_path),"rollback_sha256":rollback_sha,"dry_run_path":str(dry_run_path),"writer_self_test":writer_tests,"patch_self_test":patch_tests,"active":readback.get("active"),"secret_scan":"passed","automatic_rollback":"not_required"};audit=write_audit(result);result["audit_path"]=str(audit);return result

def rollback(path_value):
    candidate=Path(path_value).resolve();rollback_root=ROLLBACK_ROOT.resolve()
    if rollback_root not in candidate.parents or not candidate.is_file():raise RepairError("Rollback path must be under the governed rollback root")
    workflow=json.loads(candidate.read_text());
    if workflow.get("id")!=WORKFLOW_ID or workflow.get("name")!=WORKFLOW_NAME:raise RepairError("Rollback export identity mismatch")
    current=fetch_live();before_sha=canonical_hash(current);put_workflow(workflow);readback=fetch_live();after_sha=canonical_hash(readback);expected=canonical_hash(workflow)
    if after_sha!=expected:raise RepairError("Rollback read-back SHA mismatch")
    result={"ok":True,"mode":"rollback","workflow_id":WORKFLOW_ID,"before_sha256":before_sha,"after_sha256":after_sha,"rollback_source":str(candidate)};audit=write_audit(result);result["audit_path"]=str(audit);return result

def main():
    parser=argparse.ArgumentParser();mode=parser.add_mutually_exclusive_group(required=True);mode.add_argument("--dry-run",action="store_true");mode.add_argument("--apply",action="store_true");mode.add_argument("--rollback");mode.add_argument("--self-test",action="store_true");args=parser.parse_args()
    try:
        if args.self_test:result={"ok":True,"writer":run_writer_self_test(),"patch":patch_self_test()}
        elif args.dry_run:result=dry_run()
        elif args.apply:result=apply_repair()
        else:result=rollback(str(args.rollback))
        print(json.dumps(result,ensure_ascii=False,separators=(",",":")));return 0
    except Exception as exc:
        print(json.dumps({"ok":False,"error_type":type(exc).__name__,"error":str(exc)},ensure_ascii=False,separators=(",",":")),file=sys.stderr);return 2

if __name__=="__main__":raise SystemExit(main())
