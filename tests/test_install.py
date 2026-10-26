"""Installer filesystem/rollback tests; no account or network access."""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PACKAGE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('release_installer',PACKAGE/'install.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.bundle=self.root/'bundle';self.parent=self.root/'personal'/'skills'
        self.source=self.bundle
        self.source.mkdir(parents=True)
        (self.source/'SKILL.md').write_text('---\nname: anti-defensive-writing\ndescription: Test fixture\n---\nOffline fixture.',encoding='utf-8')
        self.expected=installer.files(self.source)
        (self.bundle/'release-manifest.json').write_text(json.dumps({'skill_files':self.expected}),encoding='utf-8')
        self.patch=patch.object(installer,'ROOT',self.bundle);self.patch.start();self.addCleanup(self.patch.stop)
    def invoke(self,**kwargs):return installer.install(self.parent,**kwargs)
    def test_new_install_and_idempotence_verify_exact_files(self):
        a=self.invoke();self.assertEqual(a['status'],'installed')
        self.assertEqual(installer.files(self.parent/installer.NAME),self.expected)
        self.assertEqual(self.invoke()['status'],'already_installed')
        self.assertEqual(list((self.parent.parent/'skill-backups').glob('*')),[])
    def test_dry_run_does_not_create_destination(self):
        self.assertEqual(self.invoke(dry_run=True)['status'],'dry_run')
        self.assertFalse(self.parent.exists())
    def test_default_is_current_documented_user_skill_directory(self):
        task_home=self.root/'user'
        with patch.object(Path,'home',return_value=task_home):result=installer.install(dry_run=True)
        self.assertEqual(Path(result['destination']),task_home/'.agents'/'skills'/installer.NAME)
    def test_different_version_not_overwritten_without_flag(self):
        self.invoke();target=self.parent/installer.NAME
        (target/'SKILL.md').write_text('Old user version',encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError,'different version'):self.invoke()
        self.assertEqual((target/'SKILL.md').read_text(encoding='utf-8'),'Old user version')
    def test_replace_preserves_old_bytes_outside_discovery_directory(self):
        self.invoke();target=self.parent/installer.NAME
        (target/'SKILL.md').write_bytes(b'old user version')
        unrelated=self.parent/'another-skill';unrelated.mkdir();(unrelated/'keep.txt').write_bytes(b'keep')
        result=self.invoke(replace=True);backup=Path(result['backup'])
        self.assertEqual((backup/'SKILL.md').read_bytes(),b'old user version')
        self.assertEqual(backup.parent,self.parent.parent/'skill-backups')
        self.assertEqual(installer.files(target),self.expected)
        self.assertEqual((unrelated/'keep.txt').read_bytes(),b'keep')
    def test_tampered_payload_stops_before_destination_writes(self):
        (self.source/'SKILL.md').write_text('tampered',encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError,'manifest'):self.invoke()
        self.assertFalse(self.parent.exists())
    def test_overlapping_install_refused(self):
        with self.assertRaisesRegex(RuntimeError,'overlaps'):installer.install(self.bundle/'skills')
    def test_failure_after_backup_rolls_back_original(self):
        self.invoke();target=self.parent/installer.NAME
        (target/'SKILL.md').write_bytes(b'old user version')
        original=Path.rename
        def fail_staged(path,destination):
            if path.parent.name.startswith('.anti-defensive-writing-stage-'):
                raise OSError('Synthetic final rename failure')
            return original(path,destination)
        with patch.object(Path,'rename',new=fail_staged):
            with self.assertRaisesRegex(OSError,'Synthetic'):self.invoke(replace=True)
        self.assertEqual((target/'SKILL.md').read_bytes(),b'old user version')
    def test_old_python_is_rejected(self):
        with patch.object(installer.sys,'version_info',(3,10,9)):
            with self.assertRaisesRegex(RuntimeError,'3.11'):self.invoke()
        self.assertFalse(self.parent.exists())
    def test_root_developer_files_are_not_installed(self):
        (self.bundle/'tests').mkdir()
        (self.bundle/'tests/test_example.py').write_text('developer test',encoding='utf-8')
        (self.bundle/'README.md').write_text('developer notes',encoding='utf-8')
        self.invoke()
        self.assertEqual(installer.files(self.parent/installer.NAME),self.expected)
    def test_unlisted_runtime_file_stops_before_writes(self):
        (self.bundle/'scripts').mkdir()
        (self.bundle/'scripts/unlisted.py').write_text('extra',encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError,'manifest'):self.invoke()
        self.assertFalse(self.parent.exists())
    def test_manifest_paths_cannot_escape_root_or_include_developer_files(self):
        for rel in ('../outside.txt','/outside.txt','C:/outside.txt','scripts/../outside.txt',
                'scripts\\outside.py','scripts//outside.py','tests/test_example.py','README.md'):
            with self.subTest(path=rel):
                expected={**self.expected,rel:'0'*64}
                (self.bundle/'release-manifest.json').write_text(json.dumps({'skill_files':expected}),encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError,'manifest path'):self.invoke()
                self.assertFalse(self.parent.exists())

if __name__=='__main__':unittest.main()
