"""The shared library: resumes and cover letters people in the group gave as examples, with what they got
(tier 1: just a resume, up to tier 4: got a FAANG interview). They're examples of structure and tone for writing
yours, never facts about you: those only come from your own profile and resume.

LIBRARY_DIR (.env) is a clone of the group's library repo, the same folder for everyone on a shared server:

    library.yaml              one entry per document: file, kind, tier, owner, note, added, summary, roles
    resumes/<id>.txt          text only, with names, emails, phones, links and addresses taken out
    cover_letters/<id>.txt

    python library.py add <pdf> --kind resume --tier 3 --owner krish [--note "Google interview"] [--name "Krish N"]
    python library.py list
    python library.py remove <file>      e.g. resumes/krish-2026-10-29.txt
"""
import argparse
import re
import subprocess
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel

from config import PROFILE, env
from filters import words

KINDS = {"resume": "resumes", "cover_letter": "cover_letters"}
ROLES = ["software", "ml", "data", "infra", "security", "mobile", "frontend", "hardware", "embedded", "quant",
         "product", "research", "design", "business"]
MIN_TIER = 2  # tier 1 hasn't got anything yet, so it isn't an example of what works


def folder():
    return Path(env("LIBRARY_DIR") or "").expanduser() if env("LIBRARY_DIR") else None


def entries(root=None):
    root = root or folder()
    path = root / "library.yaml" if root else None
    return (yaml.safe_load(path.read_text()) or []) if path and path.exists() else []


def pdf_text(path):
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages).strip()


EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
LINK = re.compile(r"(https?://|www\.)\S+|\b(linkedin\.com|github\.com|gitlab\.com|twitter\.com|x\.com)/\S*"
                  r"|\b[\w-]+\.(dev|me|io|xyz|site|page)\b(/\S*)?", re.I)
PHONE = re.compile(r"\+?\(?\d[\d \t().-]{8,}\d")
STREET = re.compile(r"\b\d+\s+[A-Z][\w.]*(\s+[A-Z][\w.]*)*\s+(St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|"
                    r"Ln|Lane|Way|Cres|Crescent|Ct|Court|Pl|Place)\b\.?[^\n]*")


def redact(text, names=(), kind="resume"):
    """The text with whatever says who wrote it taken out: emails, links, phone numbers (10+ digits, so date
    ranges like 2023-2024 stay), street addresses, the given names, and a resume's first line (its name header)."""
    text = EMAIL.sub("[email]", text)
    text = LINK.sub("[link]", text)
    text = PHONE.sub(lambda m: "[phone]" if 10 <= sum(c.isdigit() for c in m[0]) <= 15 else m[0], text)
    text = STREET.sub("[address]", text)
    for name in {part for full in names for part in [full, *full.split()] if len(part) > 1}:
        text = re.sub(rf"\b{re.escape(name)}\b", "[name]", text, flags=re.I)
    lines = text.splitlines()
    if kind == "resume" and lines and len(lines[0].split()) <= 4 and not re.search(r"\d", lines[0]):
        lines[0] = "[name]"
    return "\n".join(lines).strip()


class Card(BaseModel):
    summary: str
    roles: list[str]


def describe(text, kind):
    """One or two sentences on what this document is (field, level, strongest points) and its role families."""
    import llm

    return llm._ask("library", f"This is an anonymised {kind.replace('_', ' ')} from a shared library of examples.\n\n"
                    f"{text[:8000]}\n\nSummarise in 1-2 sentences: the field, level (intern / new grad / "
                    f"experienced) and what stands out. roles: the ones that fit, from {ROLES}.",
                    Card, model=llm.FAST_MODEL, effort="low")


def examples(kind, job, posting="", k=3, docs=None):
    """The k library documents of `kind` most like this job, better tiers first among similar ones: (entry, text).
    Relevance is shared words with the title (counted most) and the posting; the tier scales it."""
    root = folder()
    if docs is None:
        docs = [(e, (root / e["file"]).read_text()) for e in entries(root)
                if e.get("kind") == kind and (root / e["file"]).exists()] if root else []
    title, ad = words(job["title"]), words(posting[:6000])

    def score(doc):
        entry, text = doc
        have = words(f"{' '.join(entry.get('roles', []))} {entry.get('summary', '')} {text}")
        return (3 * len(title & have) + len(ad & have) / 10) * (1 + (entry.get("tier", 1) - 1) / 4)

    ranked = sorted((d for d in docs if d[0].get("tier", 1) >= MIN_TIER), key=score, reverse=True)
    return [d for d in ranked[:k] if score(d) > 0]


def _git(root, *args):
    if (root / ".git").exists():
        subprocess.run(["git", "-C", str(root), *args], check=True)


def sync():
    """Pull the group's latest library (if LIBRARY_DIR is a git clone). Called once a day by the bot."""
    if (root := folder()) and (root / ".git").exists():
        subprocess.run(["git", "-C", str(root), "pull", "--ff-only", "-q"], check=True, timeout=120)


def _save(root, docs, message):
    (root / "library.yaml").write_text(yaml.safe_dump(docs, sort_keys=False, allow_unicode=True, width=120))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    _git(root, "push", "-q")


def add(pdf, kind, tier, owner, note="", names=(), yes=False):
    root = folder()
    if not root or not root.is_dir():
        raise SystemExit("Set LIBRARY_DIR in .env to your clone of the library repo.")
    _git(root, "pull", "--ff-only", "-q")
    text = redact(pdf_text(pdf), names, kind)
    if len(text) < 200:
        raise SystemExit(f"Only got {len(text)} characters out of {pdf}. Is it a scanned image? Export it as text PDF.")
    print(text, "\n" + "-" * 60)
    if not yes and input("This is exactly what goes in the shared library. Add it? [y/N] ").strip().lower() != "y":
        raise SystemExit("Not added.")
    docs = entries(root)
    stem = f"{re.sub(r'[^a-z0-9]+', '-', owner.lower()).strip('-')}-{date.today().isoformat()}"
    file = f"{KINDS[kind]}/{stem}.txt"
    n = 2
    while (root / file).exists():
        file, n = f"{KINDS[kind]}/{stem}-{n}.txt", n + 1
    card = describe(text, kind)
    (root / file).parent.mkdir(exist_ok=True)
    (root / file).write_text(text + "\n")
    docs.append({"file": file, "kind": kind, "tier": tier, "owner": owner, "note": note,
                 "added": date.today().isoformat(), "summary": card.summary,
                 "roles": [r for r in card.roles if r in ROLES]})
    _save(root, docs, f"Add {file} (tier {tier})")
    print(f"Added {file}: {card.summary}")


def remove(file):
    root = folder()
    _git(root, "pull", "--ff-only", "-q")
    docs = entries(root)
    if not any(e["file"] == file for e in docs):
        raise SystemExit(f"No {file} in library.yaml.")
    (root / file).unlink(missing_ok=True)
    _save(root, [e for e in docs if e["file"] != file], f"Remove {file}")
    print(f"Removed {file}. (It's still in the repo's git history; rewrite history to erase it completely.)")


def summary():
    """For /library: how many of each kind and tier, and whether LIBRARY_DIR is set."""
    if not folder():
        return "No library: set LIBRARY_DIR in .env to your clone of the group's library repo."
    docs = entries()
    if not docs:
        return f"The library in {folder()} is empty. Add with: python library.py add <pdf> --kind resume --tier 3 ..."
    lines = [f"{len(docs)} documents in the library ({folder()}):"]
    for kind in KINDS:
        tiers = [e.get("tier", 1) for e in docs if e.get("kind") == kind]
        if tiers:
            lines.append(f"{kind.replace('_', ' ')}s: " + ", ".join(f"tier {t}: {tiers.count(t)}"
                                                                     for t in sorted(set(tiers), reverse=True)))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="The group's shared library of example resumes and cover letters.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add a PDF (personal details taken out; you see the text before it's added)")
    a.add_argument("pdf", type=Path)
    a.add_argument("--kind", choices=KINDS, required=True)
    a.add_argument("--tier", type=int, choices=[1, 2, 3, 4], required=True,
                   help="1: just a resume, 4: got a FAANG interview (cover letters: passed)")
    a.add_argument("--owner", required=True, help="whose it is, so they can find and remove it")
    a.add_argument("--note", default="", help="what it got, e.g. 'Google SWE intern interview'")
    a.add_argument("--name", action="append", default=[],
                   help="a name to take out (repeat for several); default: the one in your profile.yaml")
    a.add_argument("--yes", action="store_true", help="don't ask before adding")
    sub.add_parser("list")
    r = sub.add_parser("remove")
    r.add_argument("file")
    args = parser.parse_args()
    if args.cmd == "add":
        names = args.name or [f"{PROFILE.get('first_name', '')} {PROFILE.get('last_name', '')}".strip()]
        add(args.pdf, args.kind, args.tier, args.owner, args.note, [n for n in names if n], args.yes)
    elif args.cmd == "list":
        print(summary())
        for e in entries():
            print(f"  {e['file']}  tier {e.get('tier')}  {e.get('owner')}  {e.get('note') or ''}  {e.get('summary', '')}")
    else:
        remove(args.file)


if __name__ == "__main__":
    main()
