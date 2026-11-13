"""Pluggable LLM providers + transcription + external sinks (wiki, todoist)."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path

PROMPT = """You are processing a solo voice monologue by the user.
Transcript (speech-to-text, may contain errors):
<transcript>
{t}
</transcript>
Return ONLY a JSON object with keys:
 "title": short descriptive title (<=8 words),
 "cleaned": transcript with filler words removed, errors fixed, paragraphs added; keep his voice,
 "summary": 2-5 sentence summary,
 "action_items": list of concrete tasks he committed to (imperative, short; [] if none),
 "tags": up to 5 lowercase topic tags.
"""


def require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"{name} is not set (no built-in default; see README)")
    return v


def wiki_base() -> str:
    return require_env("WIKIJS_URL").rstrip("/")


def load_env() -> None:
    """Load KEY=VALUE files listed in OMI_ENV_FILES (colon-separated; real env wins)."""
    for p in filter(None, os.environ.get("OMI_ENV_FILES", "").split(":")):
        f = Path(p).expanduser()
        if f.exists():
            for line in f.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _post(url: str, body: bytes, headers: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


class AnthropicLLM:
    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("OMI_ANTHROPIC_MODEL", "claude-sonnet-4-5")

    def complete(self, prompt: str) -> str:
        key = os.environ["ANTHROPIC_API_KEY"]
        d = _post("https://api.anthropic.com/v1/messages",
                  json.dumps({"model": self.model, "max_tokens": 8000,
                              "messages": [{"role": "user", "content": prompt}]}).encode(),
                  {"x-api-key": key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"})
        return "".join(b.get("text", "") for b in d["content"])


class OllamaLLM:
    def __init__(self, model: str | None = None, url: str | None = None):
        self.model = model or os.environ.get("OMI_OLLAMA_MODEL", "qwen2.5:14b")
        self.url = url or require_env("OMI_OLLAMA_URL")

    def complete(self, prompt: str) -> str:
        d = _post(f"{self.url}/api/generate",
                  json.dumps({"model": self.model, "prompt": prompt, "stream": False,
                              "format": "json"}).encode(),
                  {"content-type": "application/json"}, timeout=900)
        return d["response"]


PROVIDERS = {"anthropic": AnthropicLLM, "ollama": OllamaLLM}


def get_llm(name: str, model: str | None = None):
    return PROVIDERS[name](model)


def transcribe(path: Path, url: str | None, model: str) -> str:
    if not url:
        raise RuntimeError("OMI_WHISPER_URL / --whisper-url is not set (no built-in default)")
    boundary = "omiboundary7f3a"
    parts = []
    for k, v in (("model", model), ("response_format", "json"), ("language", "en")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n'.encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    d = _post(url, b"".join(parts), {"content-type": f"multipart/form-data; boundary={boundary}"}, 1800)
    return d["text"].strip()


def transcribe_local(path: Path, model: str) -> str:
    """CPU fallback via the faster-whisper package (optional extra `local`)."""
    from faster_whisper import WhisperModel  # type: ignore
    import numpy as np  # faster-whisper dependency
    # Decode with system ffmpeg (avoids PyAV API drift; handles m4a/opus/mp3).
    pcm = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), "-f", "s16le",
                          "-ac", "1", "-ar", "16000", "-"], capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    m = WhisperModel(model, device="cpu", compute_type="int8")
    segs, _ = m.transcribe(audio, language="en")
    return " ".join(s.text.strip() for s in segs).strip()


# ---- Wiki.js -------------------------------------------------------------
WIKI_PUSH = Path(os.environ.get("OMI_WIKI_PUSH_SCRIPT", "wiki_push.py")).expanduser()


def _gql(query: str, variables: dict | None = None) -> dict:
    url = wiki_base()
    if not url.endswith("/graphql"):
        url += "/graphql"
    return _post(url, json.dumps({"query": query, "variables": variables or {}}).encode(),
                 {"content-type": "application/json",
                  "authorization": f"Bearer {os.environ['WIKIJS_TOKEN']}"}, 60)


def wiki_exists(path: str) -> bool:
    d = _gql('query($p:String!){pages{singleByPath(path:$p,locale:"en"){id}}}', {"p": path})
    return bool(((d.get("data") or {}).get("pages") or {}).get("singleByPath"))


def free_wiki_path(base: str) -> str:
    """No-clobber: wiki_push.py overwrites, so pick base, base-2, base-3..."""
    p, n = base, 1
    while wiki_exists(p):
        n += 1
        p = f"{base}-{n}"
    return p


def wiki_create(path: str, md_file: Path, title: str, tags: list[str]) -> dict:
    """Create a page via GraphQL (container mode: no wiki_push.py). Refuses to overwrite."""
    if wiki_exists(path):
        raise RuntimeError(f"wiki page {path} already exists (no-clobber)")
    q = ("mutation($c:String!,$p:String!,$t:String!,$g:[String]!){pages{create(content:$c,description:\"\","
         "editor:\"markdown\",isPublished:true,isPrivate:false,locale:\"en\",path:$p,tags:$g,title:$t)"
         "{responseResult{succeeded message} page{id}}}}")
    d = _gql(q, {"c": md_file.read_text(), "p": path, "t": title, "g": tags})
    r = ((d.get("data") or {}).get("pages") or {}).get("create") or {}
    if not (r.get("responseResult") or {}).get("succeeded"):
        raise RuntimeError(f"wiki create failed: {d}")
    return {"action": "created", "id": (r.get("page") or {}).get("id"), "path": path}


def wiki_publish(path: str, md_file: Path, title: str, tags: list[str]) -> dict:
    if not WIKI_PUSH.exists() or os.environ.get("OMI_WIKI_MODE") == "graphql":
        return wiki_create(path, md_file, title, tags)
    out = subprocess.run([str(WIKI_PUSH), "--path", path, "--file", str(md_file), "--title", title,
                          "--tags", ",".join(tags), "--published", "--unsafe-any-path", "--json"],
                         capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        raise RuntimeError(f"wiki_push failed rc={out.returncode}: {out.stderr[-500:]}")
    res = json.loads(out.stdout.strip().splitlines()[-1])
    if res.get("action") != "created":
        raise RuntimeError(f"wiki_push did not create page (no-clobber violated?): {res}")
    return res


def wiki_list_paths(prefix: str) -> list[dict]:
    """Every page under <prefix>/ (source of truth for the index). [{id,path,title,createdAt}]"""
    d = _gql('query{pages{list(limit:100000,orderBy:CREATED,orderByDirection:DESC){id path title createdAt locale}}}')
    pages = ((d.get("data") or {}).get("pages") or {}).get("list")
    if pages is None:
        raise RuntimeError(f"wiki list failed: {str(d)[:300]}")
    return [p for p in pages if p.get("path", "").startswith(prefix + "/") and p.get("locale", "en") == "en"]


def _wiki_get(path: str) -> dict | None:
    d = _gql('query($p:String!){pages{singleByPath(path:$p,locale:"en"){id content}}}', {"p": path})
    return ((d.get("data") or {}).get("pages") or {}).get("singleByPath")


def wiki_write_index(path: str, content: str, title: str) -> dict:
    """Create or replace the index page at exactly <path>, then verify via singleByPath.
    Only ever writes this one path. Wiki.js create/update can take >6 s: 120 s timeouts."""
    page = _wiki_get(path)
    if page and (page.get("content") or "").strip() == content.strip():
        return {"action": "unchanged", "id": page["id"]}
    if not page:
        q = ("mutation($c:String!,$p:String!,$t:String!){pages{create(content:$c,description:\"Index of monologue pages\","
             "editor:\"markdown\",isPublished:true,isPrivate:false,locale:\"en\",path:$p,"
             "tags:[\"monologue\",\"index\"],title:$t){responseResult{succeeded message}}}}")
        r = _gql_slow(q, {"c": content, "p": path, "t": title})
        action = "created"
    else:
        full = _gql('query($id:Int!){pages{single(id:$id){description editor locale isPublished isPrivate title tags{tag}}}}',
                    {"id": int(page["id"])})["data"]["pages"]["single"]
        q = ("mutation($id:Int!,$c:String!,$d:String!,$e:String!,$l:String!,$pub:Boolean!,$pr:Boolean!,"
             "$tg:[String]!,$t:String!){pages{update(id:$id,content:$c,description:$d,editor:$e,locale:$l,"
             "isPublished:$pub,isPrivate:$pr,tags:$tg,title:$t){responseResult{succeeded message}}}}")
        r = _gql_slow(q, {"id": int(page["id"]), "c": content, "d": full.get("description") or "",
                          "e": full.get("editor") or "markdown", "l": full.get("locale") or "en",
                          "pub": True, "pr": bool(full.get("isPrivate")),
                          "tg": [t["tag"] for t in full.get("tags") or []] or ["monologue", "index"],
                          "t": full.get("title") or title})
        action = "updated"
    after = _wiki_get(path)   # verify; the mutation response alone is not trusted
    if not after or (after.get("content") or "").strip() != content.strip():
        raise RuntimeError(f"index {action} not visible on re-read (response: {str(r)[:300]})")
    return {"action": action, "id": after["id"]}


def _gql_slow(query: str, variables: dict) -> dict:
    url = wiki_base()
    if not url.endswith("/graphql"):
        url += "/graphql"
    try:
        return _post(url, json.dumps({"query": query, "variables": variables}).encode(),
                     {"content-type": "application/json",
                      "authorization": f"Bearer {os.environ['WIKIJS_TOKEN']}"}, 120)
    except Exception as e:  # a timeout may still have applied the write; the re-read decides
        return {"error": str(e)}


# ---- Todoist -------------------------------------------------------------
def todoist_inbox_id() -> str:
    req = urllib.request.Request("https://api.todoist.com/api/v1/projects",
                                 headers={"authorization": f"Bearer {os.environ['TODOIST_TOKEN']}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        for p in json.loads(r.read())["results"]:
            if p.get("inbox_project"):
                return p["id"]
    raise RuntimeError("no Todoist inbox")


def todoist_create(content: str, description: str, project_id: str, label: str) -> dict:
    return _post("https://api.todoist.com/api/v1/tasks",
                 json.dumps({"content": content, "description": description,
                             "project_id": project_id, "labels": [label]}).encode(),
                 {"content-type": "application/json",
                  "authorization": f"Bearer {os.environ['TODOIST_TOKEN']}"}, 15)
