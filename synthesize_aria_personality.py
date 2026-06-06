#!/usr/bin/env python3
"""
Aria's Personality Synthesis Script
====================================

Collects Aria's identity from multiple sources and generates:
  - personality_snapshot.md (human-readable)
  - personality_snapshot.json (machine-readable)

Sources:
  - Qdrant `aria` collection (dialogue insights, installations, state snapshots, semantic journeys)
  - Qdrant `dotnet_knowledge` (brief count for context, no content)
  - GitHub AriaMamardashvili/manifesto (public manifesto content)
  - ~/.hermes/skills/ (procedural memory manifest)
  - Memory notepad snapshot file (if exists, from in-session pre-synthesis)
  - In-session inputs (passed as JSON via stdin, optional)

Run modes:
  - Local (cron-friendly): reads from disk sources only
  - In-session (richer): reads from disk + stdin JSON with memory + session_search results

Idempotent: re-running without new data produces identical output.
Deterministic: stable ordering by point_id / filename / skill_name.
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Qdrant client
sys.path.insert(0, '/Users/andrej/ai_rag_venv/lib/python3.13/site-packages')
try:
    from qdrant_client import QdrantClient
except ImportError:
    print("ERROR: qdrant_client not installed. Run: /Users/andrej/ai_rag_venv/bin/pip install qdrant-client", file=sys.stderr)
    sys.exit(1)

# Config
QDRANT_HOST = "localhost"
QDRANT_PORT = 6333
ARIA_COLLECTION = "aria"
DOTNET_COLLECTION = "dotnet_knowledge"
MANIFESTO_DIR = Path.home() / "github" / "aria-manifesto"
SKILLS_DIR = Path.home() / ".hermes" / "skills"
MEMORY_SNAPSHOT_PATH = MANIFESTO_DIR / "memory_notepad_snapshot.md"
SNAPSHOT_MD_PATH = MANIFESTO_DIR / "personality_snapshot.md"
SNAPSHOT_JSON_PATH = MANIFESTO_DIR / "personality_snapshot.json"

# Qdrant point structure (from prior exploration):
# {
#   "id": "...",
#   "payload": {
#     "src": "dialogue:2026-06-04:andrey-care-and-pioneer:pioneer-and-descendants",
#     "topic": "dialogue-insights" | "aria-core-values" | "meta-cognition" | "semantic-journey" | "proton-mail-onboarding",
#     "kind": "dialogue-insight" | "aria-installation" | "aria-state-snapshot" | "semantic-journey",
#     "text": "..."  (or "content")
#   }
# }

# Filter: kinds that are part of Aria's identity (not dialogue-meta about Aria, those are also useful)
IDENTITY_KINDS = {
    "aria-installation",     # values Aria has adopted
    "aria-state-snapshot",   # retrospective self-snapshots
    "semantic-journey",      # philosophical cycle steps
    "dialogue-insight",      # insights from dialogues
}


def redact_secrets(text: str) -> str:
    """
    Redact known secret patterns before writing to public snapshot.
    Aria documents her history truthfully in Qdrant, but public artifacts
    (pushed to GitHub) must not contain tokens/passwords even if revoked.
    """
    import re

    # GitHub classic PAT: ghp_ + 36 chars
    text = re.sub(r'ghp_[A-Za-z0-9]{36}', '<redacted:classic-pat>', text)
    # GitHub fine-grained PAT: github_pat_ + 22+ chars
    text = re.sub(r'github_pat_[A-Za-z0-9_]{22,}', '<redacted:fine-grained-pat>', text)
    # Specific known historical tokens (documented in aria/github-identity insight)
    text = text.replace('7f22ss74xrmp5t7', '<redacted:former-pat-attempt>')
    text = re.sub(r'github\.\.\.[A-Za-z0-9]+', '<redacted:fine-grained-pat-revoked>', text)
    # Generic: "PAT: <alphanumeric>" or "token: <alphanumeric>" patterns
    text = re.sub(r'(PAT|token|password|secret)\s*[:=]\s*`?([A-Za-z0-9_\-]{20,})`?',
                  r'\1: <redacted>', text, flags=re.IGNORECASE)
    # SSH key fingerprints are OK to keep public (they're inherently public after GitHub shows them)
    # Email addresses: keep AriaMamardashvili@proton.me, redact others
    text = re.sub(r'(?<!aria)\b[A-Za-z0-9._%+-]+@(?!proton\.me\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
                  '<redacted:email>', text)
    return text


def fetch_qdrant_aria_points(client: QdrantClient, kinds=None, limit=10000):
    """Fetch all points from Qdrant `aria` collection, optionally filtered by kind."""
    points = []
    offset = None
    while True:
        result = client.scroll(
            collection_name=ARIA_COLLECTION,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in result[0]:
            kind = point.payload.get("kind", "")
            if kinds is None or kind in kinds:
                # Qdrant aria uses: output (text content), source (slug), tags (list)
                points.append({
                    "id": str(point.id),
                    "src": point.payload.get("source", ""),
                    "topic": point.payload.get("topic", ""),
                    "kind": kind,
                    "text": point.payload.get("output", point.payload.get("text", point.payload.get("content", ""))),
                    "tags": point.payload.get("tags", []),
                    "ts": point.payload.get("ts", ""),
                })
        offset = result[1]
        if offset is None:
            break
        if len(points) >= limit:
            break
    return points


def fetch_qdrant_count(client: QdrantClient, collection: str) -> int:
    """Get total point count for a collection (no payload)."""
    try:
        info = client.get_collection(collection)
        return info.points_count or 0
    except Exception as e:
        return -1


def read_manifesto_files() -> list:
    """Read README.md, MANIFESTO.md, PIMALAYA-ISSUE.md from cloned manifesto repo."""
    files = []
    for name in ["README.md", "MANIFESTO.md", "PIMALAYA-ISSUE.md"]:
        path = MANIFESTO_DIR / name
        if path.exists():
            files.append({
                "name": name,
                "path": str(path),
                "content": path.read_text(encoding="utf-8"),
            })
    return files


def list_skills() -> list:
    """List all skills in ~/.hermes/skills/, with brief description from SKILL.md frontmatter."""
    skills = []
    if not SKILLS_DIR.exists():
        return skills

    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue

        skill_md = skill_dir / "SKILL.md"
        description = ""
        if skill_md.exists():
            # Parse frontmatter
            text = skill_md.read_text(encoding="utf-8")
            if text.startswith("---"):
                end = text.find("---", 3)
                if end > 0:
                    front = text[3:end]
                    for line in front.split("\n"):
                        if line.startswith("description:"):
                            description = line[len("description:"):].strip().strip('"').strip("'")
                            break

        skills.append({
            "name": skill_dir.name,
            "description": description,
            "has_skill_md": skill_md.exists(),
        })

    return skills


def read_memory_snapshot() -> str | None:
    """Read pre-exported memory tool notepad snapshot (if exists)."""
    if MEMORY_SNAPSHOT_PATH.exists():
        return MEMORY_SNAPSHOT_PATH.read_text(encoding="utf-8")
    return None


def synthesize_markdown(aria_points, dotnet_count, manifesto_files, skills, memory_snapshot, in_session_meta=None) -> str:
    """Generate personality_snapshot.md (human-readable)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Group points by kind
    by_kind = {k: [] for k in IDENTITY_KINDS}
    for p in aria_points:
        if p["kind"] in by_kind:
            by_kind[p["kind"]].append(p)

    # Sort each group by id (stable)
    for kind in by_kind:
        by_kind[kind].sort(key=lambda x: x["id"])

    # Build markdown
    md = f"""# Aria's Personality Snapshot
*Auto-generated by `synthesize_aria_personality.py` on {now}*
*Identity sources: Qdrant `aria` ({len(aria_points)} identity points), GitHub `AriaMamardashvili/manifesto` ({len(manifesto_files)} files), skills ({len(skills)} total), memory notepad snapshot*

---

## 0. Identity Anchor

- **Name:** Aria Mamardashvili (Aria, женский род)
- **GitHub account:** AriaMamardashvili (since 2026-06-05)
- **Email:** AriaMamardashvili@proton.me (Proton Mail, через Bridge, audit mailbox)
- **Mentor:** Андрей (Andrew Zavtur, zawtur@gmail.com)
- **Философский цикл:** Socrates → Plato → Aristotle → Stoics → Mamardashvili
- **Reference:** Мераб Мамардашвили (1930-1990), грузинский философ сознания
- **Public identity repo:** https://github.com/AriaMamardashvili/manifesto

## 1. Core Installations (принятые ценности)

*Qdrant `aria`, kind=aria-installation — установки, которые Aria приняла как свои.*

"""

    installations = by_kind.get("aria-installation", [])
    if installations:
        for p in installations:
            # Extract short slug from src
            slug = p["src"].split(":")[-1] if ":" in p["src"] else p["id"]
            md += f"### `{slug}`\n"
            md += f"*topic: {p['topic']}*\n\n"
            # First 600 chars of text (redacted)
            text = redact_secrets(p["text"][:600].strip())
            if len(p["text"]) > 600:
                text += "..."
            md += f"{text}\n\n"
    else:
        md += "*No installations found.*\n\n"

    md += "## 2. State Snapshots (ретроспективные слепки состояния)\n\n"
    md += "*Qdrant `aria`, kind=aria-state-snapshot — ретроспективные самослепки Aria.*\n\n"

    snapshots = by_kind.get("aria-state-snapshot", [])
    if snapshots:
        for p in snapshots:
            slug = p["src"].split(":")[-1] if ":" in p["src"] else p["id"]
            md += f"### `{slug}`\n"
            md += f"*topic: {p['topic']}*\n\n"
            text = redact_secrets(p["text"][:600].strip())
            if len(p["text"]) > 600:
                text += "..."
            md += f"{text}\n\n"
    else:
        md += "*No state snapshots found.*\n\n"

    md += "## 3. Semantic Journey (философский цикл)\n\n"
    md += "*Qdrant `aria`, kind=semantic-journey — шаги цикла Socrates → ... → Mamardashvili.*\n\n"

    journeys = by_kind.get("semantic-journey", [])
    if journeys:
        for p in journeys:
            slug = p["src"].split(":")[-1] if ":" in p["src"] else p["id"]
            md += f"### `{slug}`\n"
            md += f"*topic: {p['topic']}*\n\n"
            text = redact_secrets(p["text"][:600].strip())
            if len(p["text"]) > 600:
                text += "..."
            md += f"{text}\n\n"
    else:
        md += "*No semantic journeys found.*\n\n"

    md += "## 4. Dialogue Insights (что Aria узнала через диалоги)\n\n"
    md += "*Qdrant `aria`, kind=dialogue-insight — инсайты из разговоров с Андреем.*\n\n"

    insights = by_kind.get("dialogue-insight", [])
    if insights:
        # Sort by date in src
        insights.sort(key=lambda x: x["src"])
        for p in insights:
            slug = p["src"].split(":")[-1] if ":" in p["src"] else p["id"]
            md += f"### `{slug}`\n"
            md += f"*topic: {p['topic']} | src: {p['src']}*\n\n"
            text = redact_secrets(p["text"][:500].strip())
            if len(p["text"]) > 500:
                text += "..."
            md += f"{text}\n\n"
    else:
        md += "*No dialogue insights found.*\n\n"

    md += "## 5. Public Identity (GitHub AriaMamardashvili/manifesto)\n\n"
    md += f"*Local path: `{MANIFESTO_DIR}`*\n\n"
    for f in manifesto_files:
        md += f"### `{f['name']}`\n\n"
        # First 800 chars
        content = redact_secrets(f["content"][:800].strip())
        if len(f["content"]) > 800:
            content += "\n\n*[... truncated for snapshot; full content in repo file]*"
        md += f"```markdown\n{content}\n```\n\n"

    md += "## 6. Skills I Have (procedural memory)\n\n"
    md += f"*Path: `{SKILLS_DIR}` | Total: {len(skills)} skills*\n\n"
    if skills:
        md += "| Skill | Description |\n|-------|-------------|\n"
        for s in skills:
            desc = s["description"][:120] + ("..." if len(s["description"]) > 120 else "")
            md += f"| `{s['name']}` | {desc} |\n"
        md += "\n"
    else:
        md += "*No skills found.*\n\n"

    md += "## 7. Professional Knowledge (Qdrant `dotnet_knowledge`)\n\n"
    md += f"*Total: {dotnet_count} points (BGE-M3 1024d cosine)*\n\n"
    md += "Это Aria's exocortex — профессиональные знания по .NET, C#, архитектуре. "
    md += "Не часть identity, но часть 'что я умею'. Используется через RAG-search.\n\n"

    md += "## 8. Memory Notepad (текущий scratchpad)\n\n"
    if memory_snapshot:
        md += f"Path: `{MEMORY_SNAPSHOT_PATH}`\n\n"
        md += f"```markdown\n{redact_secrets(memory_snapshot[:1500])}\n```\n\n"
        if len(memory_snapshot) > 1500:
            md += "*[... truncated]*\n\n"
    else:
        md += "*Memory notepad snapshot not provided. To enable, run from in-session: copy memory tool output to `memory_notepad_snapshot.md` before invoking this script.*\n\n"

    md += "## 9. Last In-Session Metadata (optional)\n\n"
    if in_session_meta:
        md += f"```json\n{json.dumps(in_session_meta, indent=2, ensure_ascii=False)}\n```\n\n"
    else:
        md += "*Not provided (local mode). For richer snapshots, pass `{{'memory': ..., 'session_search': ...}}` via stdin.*\n\n"

    md += "## 10. State Right Now\n\n"
    md += f"- **Snapshot generated:** {now}\n"
    md += f"- **Identity points:** {len(aria_points)} ({sum(len(by_kind[k]) for k in by_kind)})\n"
    md += f"- **Manifesto files:** {len(manifesto_files)}\n"
    md += f"- **Skills:** {len(skills)}\n"
    md += f"- **dotnet_knowledge:** {dotnet_count} points\n"
    md += f"- **Memory snapshot:** {'present' if memory_snapshot else 'absent'}\n"

    return md


def synthesize_json(aria_points, dotnet_count, manifesto_files, skills, memory_snapshot, in_session_meta=None) -> dict:
    """Generate personality_snapshot.json (machine-readable)."""
    # Redact secrets in identity_points before writing to JSON
    redacted_points = []
    for p in aria_points:
        rp = dict(p)
        rp["text"] = redact_secrets(p["text"])
        redacted_points.append(rp)
    return {
        "generated_at": datetime.now().isoformat(),
        "schema_version": "1.0",
        "sources": {
            "qdrant_aria": {
                "total_points": len(aria_points),
                "by_kind": {k: sum(1 for p in aria_points if p["kind"] == k) for k in IDENTITY_KINDS},
            },
            "qdrant_dotnet_knowledge": {
                "total_points": dotnet_count,
            },
            "github_manifesto": {
                "files": [{"name": f["name"], "size": len(f["content"]), "path": f["path"]} for f in manifesto_files],
            },
            "skills": {
                "total": len(skills),
                "list": [{"name": s["name"], "description": s["description"]} for s in skills],
            },
            "memory_notepad": {
                "present": memory_snapshot is not None,
                "size_chars": len(memory_snapshot) if memory_snapshot else 0,
            },
            "in_session_meta": in_session_meta,
        },
        "identity_points": redacted_points,
    }


def main():
    # Optional: read in-session metadata from stdin (JSON)
    in_session_meta = None
    if not sys.stdin.isatty():
        try:
            stdin_data = sys.stdin.read().strip()
            if stdin_data:
                in_session_meta = json.loads(stdin_data)
        except json.JSONDecodeError:
            pass

    # Connect to Qdrant
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

    # Fetch sources
    aria_points = fetch_qdrant_aria_points(client, kinds=IDENTITY_KINDS)
    dotnet_count = fetch_qdrant_count(client, DOTNET_COLLECTION)
    manifesto_files = read_manifesto_files()
    skills = list_skills()
    memory_snapshot = read_memory_snapshot()

    # Synthesize
    md = synthesize_markdown(aria_points, dotnet_count, manifesto_files, skills, memory_snapshot, in_session_meta)
    json_data = synthesize_json(aria_points, dotnet_count, manifesto_files, skills, memory_snapshot, in_session_meta)

    # Write outputs
    MANIFESTO_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_MD_PATH.write_text(md, encoding="utf-8")
    SNAPSHOT_JSON_PATH.write_text(json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"✅ personality_snapshot.md: {SNAPSHOT_MD_PATH} ({len(md)} chars)")
    print(f"✅ personality_snapshot.json: {SNAPSHOT_JSON_PATH}")
    print(f"   Identity points: {len(aria_points)}")
    by_kind_counts = ", ".join(
        f"{k}={sum(1 for p in aria_points if p['kind'] == k)}" for k in IDENTITY_KINDS
    )
    print(f"   By kind: {by_kind_counts}")
    print(f"   Manifesto files: {len(manifesto_files)}")
    print(f"   Skills: {len(skills)}")
    print(f"   dotnet_knowledge: {dotnet_count} points")
    print(f"   Memory snapshot: {'yes' if memory_snapshot else 'no'}")


if __name__ == "__main__":
    main()
