#!/usr/bin/env python3
"""Install the bundled Codex skill, verifying files and preserving an old version."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import shutil
import sys
import tempfile
import subprocess
import importlib.util
from pathlib import Path, PurePosixPath

ROOT=Path(__file__).resolve().parent
NAME='anti-defensive-writing'
PAYLOAD_DIRS=('agents','scripts','references')

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def files(directory):
    return {p.relative_to(directory).as_posix():digest(p) for p in sorted(directory.rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}

def payload(source,expected):
    """Verify only the installable root resources; developer files stay outside."""
    if not isinstance(expected,dict) or 'SKILL.md' not in expected:
        raise RuntimeError('Invalid skill_files manifest; SKILL.md is required.')
    for rel,checksum in expected.items():
        if not isinstance(rel,str):raise RuntimeError('Invalid manifest path.')
        path=PurePosixPath(rel)
        if (not rel or '\\' in rel or ':' in rel or path.is_absolute() or
                '..' in path.parts or path.as_posix()!=rel or
                (rel!='SKILL.md' and (len(path.parts)<2 or path.parts[0] not in PAYLOAD_DIRS))):
            raise RuntimeError('Invalid manifest path: '+rel)
        if not isinstance(checksum,str) or len(checksum)!=64 or any(c not in '0123456789abcdef' for c in checksum):
            raise RuntimeError('Invalid manifest checksum: '+rel)
    actual={}
    roots=[source/'SKILL.md',*(source/name for name in PAYLOAD_DIRS)]
    for root in roots:
        paths=[root,*root.rglob('*')] if root.is_dir() else [root]
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(source):
                raise RuntimeError('Bundled runtime path escapes its root or is a symlink.')
            if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc':
                actual[path.relative_to(source).as_posix()]=digest(path)
    if actual!=expected:
        raise RuntimeError('Bundled skill files differ from release-manifest.json; installation stopped.')
    return expected

def install(skills_dir=None,replace=False,dry_run=False):
    if sys.version_info<(3,11):raise RuntimeError('Python 3.11 or newer is required.')
    source=ROOT.resolve()
    manifest=json.loads((ROOT/'release-manifest.json').read_text(encoding='utf-8'))
    expected=payload(source,manifest['skill_files'])
    parent=Path(skills_dir).expanduser().resolve() if skills_dir else Path.home()/'.agents'/'skills'
    parent=parent.resolve();target=parent/NAME
    resolved=target.resolve()
    if source==resolved or source.is_relative_to(resolved) or resolved.is_relative_to(source):
        raise RuntimeError('Installation destination overlaps the bundled source skill.')
    if target.is_symlink():raise RuntimeError('Destination is a symlink; choose an ordinary skill directory.')
    result={'skill':NAME,'destination':str(target),'model_calls':0,'config_changes':False}
    if target.exists():
        if target.is_dir() and files(target)==expected:
            return {**result,'status':'already_installed','verified_files':len(expected)}
        if not replace:raise RuntimeError('A different version already exists. Use --replace to preserve it in a backup and install this version.')
        if not target.is_dir():raise RuntimeError('Destination is not a skill directory.')
    if dry_run:return {**result,'status':'dry_run','verified_files':len(expected),'replace_requested':replace}
    parent.mkdir(parents=True,exist_ok=True)
    backup=None
    # This installer never recursively deletes a pre-existing user directory.
    with tempfile.TemporaryDirectory(prefix='.anti-defensive-writing-stage-',dir=parent) as temp:
        staged=Path(temp)/NAME
        staged.mkdir()
        for rel in expected:
            destination=staged/rel
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source/rel,destination)
        if files(staged)!=expected:raise RuntimeError('Staged file verification failed; destination unchanged.')
        if target.exists():
            backup_root=(parent.parent/'skill-backups').resolve()
            backup_root.mkdir(parents=True,exist_ok=True)
            stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup=backup_root/(NAME+'-'+stamp)
            if backup.exists() or backup.resolve().parent!=backup_root:
                raise RuntimeError('Invalid backup destination.')
            if target.resolve().parent!=parent or target.is_symlink():
                raise RuntimeError('Destination changed before replacement; installation stopped.')
            target.rename(backup)
        try:staged.rename(target)
        except BaseException:
            if backup is not None and not target.exists():backup.rename(target)
            raise
    if files(target)!=expected:raise RuntimeError('Installed file verification failed; inspect the saved backup before proceeding.')
    return {**result,'status':'installed','verified_files':len(expected),'backup':str(backup) if backup else None}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skills-dir',help='Parent directory for skills; default ~/.agents/skills')
    parser.add_argument('--replace',action='store_true',help='Back up an existing different version outside the discovery directory')
    parser.add_argument('--dry-run',action='store_true',help='Validate and print the destination without writing')
    parser.add_argument('--with-formats',action='store_true',help='Install missing PDF/Word Python dependencies into the selected Python environment')
    parser.add_argument('--with-pandoc',action='store_true',help='Also install the local Pandoc binary for LaTeX/math conversion to Word')
    args=parser.parse_args(argv)
    try:
        result=install(args.skills_dir,args.replace,args.dry_run)
        requirements=[]
        if args.with_formats:
            missing=any(importlib.util.find_spec(n) is None for n in ('pypdf','docx','reportlab'))
            if missing:requirements.append(ROOT/'references/requirements-formats.txt')
        if args.with_pandoc:
            available=False
            if importlib.util.find_spec('pypandoc'):
                import pypandoc
                try:available=bool(pypandoc.get_pandoc_path())
                except OSError:pass
            if not available:requirements.append(ROOT/'references/requirements-pandoc.txt')
        if requirements and not args.dry_run:
            for requirement in requirements:
                subprocess.run([sys.executable,'-m','pip','--isolated','install','--index-url','https://pypi.org/simple',
                    '--disable-pip-version-check','-r',str(requirement)],check=True,shell=False,stdout=sys.stderr)
        result['dependency_install_requested']=bool(args.with_formats or args.with_pandoc)
        result['dependency_requirements']= [p.name for p in requirements]
        # ASCII JSON remains lossless through Windows PowerShell legacy code pages.
        print(json.dumps(result,ensure_ascii=True,indent=2));return 0
    except Exception as exc:
        print(json.dumps({'status':'failed','message':str(exc)},ensure_ascii=True),file=sys.stderr);return 1

if __name__=='__main__':raise SystemExit(main())
