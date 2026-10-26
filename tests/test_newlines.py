"""Preserve supplied task/source text, including Windows newlines."""
from pathlib import Path
import sys
import tempfile
import unittest
SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
from readeraware.pipeline import load_inputs
from readeraware.util import text_write

class InputTextTests(unittest.TestCase):
    def test_crlf_source_and_task_survive_loading_and_snapshot(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root=Path(folder);source=root/'draft.md';task=root/'task.txt';snapshot=root/'saved.txt'
            source_bytes='Project draft\r\nA measured result.\r\n'.encode('utf-8')
            task_bytes='Write a report.\r\nPreserve α and 标题.\r\n'.encode('utf-8')
            source.write_bytes(source_bytes);task.write_bytes(task_bytes)
            sources,request=load_inputs([source],task)
            self.assertEqual(sources[0]['text'].encode('utf-8'),source_bytes)
            self.assertEqual(request.encode('utf-8'),task_bytes)
            text_write(snapshot,request)
            self.assertEqual(snapshot.read_bytes(),task_bytes)
    def test_optional_utf8_bom_is_decoded_without_changing_newlines(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root=Path(folder);source=root/'draft.txt';task=root/'task.txt'
            body='Draft\r\nLine 2\n'
            source.write_bytes(body.encode('utf-8-sig'));task.write_bytes(body.encode('utf-8-sig'))
            sources,request=load_inputs([source],task)
            self.assertEqual(sources[0]['text'],body)
            self.assertEqual(request,body)

if __name__=='__main__':unittest.main()
