"""Main owns document tools; child calls remain Markdown-only and isolated."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
from readeraware.pipeline import run_pipeline
from readeraware.preparation import prepare_inputs,inspect_delivery
from readeraware.util import ValidationError,read_json

class FormatPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.task=self.root/'task.txt'
        self.task.write_bytes('请将中文材料写成英文正式报告，输出Word及PDF。\r\n'.encode('utf-8'))
    def args(self,source,**overrides):
        values=dict(source=[str(source)],task_file=str(self.task),out=str(self.root/'run'),threshold=None,
            future_work='auto',word_budget=None,future_words=None,backend='fixture',model=None,effort=None,
            timeout=60,codex_bin=None)
        values.update(overrides);return argparse.Namespace(**values)
    def test_prepared_chinese_source_five_calls_output_markdown_only(self):
        source=self.root/'材料.md';source.write_text('试点使重复订单减少15%。公式为 $x+y=z$。',encoding='utf-8')
        result=run_pipeline(self.args(source));out=self.root/'run'
        self.assertEqual(result['actual_backend_calls'],5)
        self.assertEqual(result['actual_real_model_calls'],0)
        self.assertEqual(result['writer_content_format'],'markdown')
        self.assertFalse(list(out.rglob('*.docx')));self.assertFalse(list(out.rglob('*.pdf')))
        self.assertTrue((out/'outputs/document.md').read_bytes().startswith((out/'outputs/main.md').read_bytes()))
        self.assertEqual((out/'inputs/task.txt').read_bytes(),self.task.read_bytes())
        for stage in result['stages']:
            packet=read_json(out/stage['directory']/'input.json')
            self.assertEqual(packet['source'][0]['text'],source.read_text(encoding='utf-8'))
            prompt=(out/stage['directory']/'request.txt').read_text(encoding='utf-8')
            self.assertIn('LANGUAGE INTERFACE ADDITION',prompt)
            if stage['stage']=='probabilities':
                self.assertEqual(set(packet),{'source','review_points'})
                self.assertNotIn('请将中文材料写成英文',prompt)
    def test_raw_rich_files_cannot_bypass_main_preparation(self):
        for ext in ('.tex','.pdf','.docx'):
            source=self.root/('unprepared'+ext);source.write_bytes(b'not prepared')
            with patch('readeraware.pipeline.CodexBackend') as backend:
                with self.assertRaisesRegex(ValidationError,'main agent prepares'):run_pipeline(self.args(source))
                backend.assert_not_called()
            self.assertFalse((self.root/'run').exists())
    def test_tex_preparation_is_not_fidelity_certificate(self):
        source=self.root/'paper.tex';source.write_text('\\documentclass{article}\n\\begin{document}中文 $x^2$.\\end{document}',encoding='utf-8')
        result=prepare_inputs([source],self.root/'prepared')
        self.assertEqual(result['status'],'requires_main_agent_verification')
        self.assertEqual(result['model_calls'],0)
        checklist=read_json(Path(result['checklist']))
        self.assertFalse(checklist['editable_equations_verified'])
        self.assertTrue(checklist['utf8_readback_passed'])
        self.assertIn('中文',Path(result['material']).read_text(encoding='utf-8'))
        provenance=read_json(Path(result['provenance']))
        self.assertEqual(Path(provenance['files'][0]['path']).read_bytes(),source.read_bytes())
    def test_export_cli_chinese_readback_without_model(self):
        main=self.root/'正文.md';main.write_bytes('# 中文报告\r\n\r\n试点结果：减少15%。'.encode('utf-8'))
        command=[sys.executable,'-B','-X','utf8',str(SCRIPTS/'run_harness.py'),'export','--main-file',str(main),
            '--output-format','markdown','--latex-check','off','--out',str(self.root/'export')]
        result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',shell=False)
        self.assertEqual(result.returncode,0,result.stderr)
        exported=json.loads(result.stdout);final=Path(exported['artifacts']['markdown']['combined'])
        self.assertEqual(final.read_bytes(),main.read_bytes())
        inspection=inspect_delivery([final])
        self.assertEqual(inspection['files'][0]['cjk_characters'],10)
        self.assertFalse(inspection['delivery_complete'])
        second=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',shell=False)
        self.assertEqual(second.returncode,1);self.assertIn('already exists',second.stderr)
    def test_bad_encoding_fails_readback(self):
        source=self.root/'bad.md';source.write_bytes(b'\xff')
        with self.assertRaises(UnicodeDecodeError):inspect_delivery([source])
        source.write_text('中文\ufffd',encoding='utf-8')
        with self.assertRaisesRegex(ValidationError,'Replacement'):inspect_delivery([source])

if __name__=='__main__':unittest.main()
