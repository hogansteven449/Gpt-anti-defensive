"""Release contract: old prompt bytes stay intact; only interfaces are appended."""
import hashlib,json
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
SKILL=ROOT
class PromptPreservationTests(unittest.TestCase):
    def test_all_five_original_byte_prefixes_are_untouched(self):
        manifest=json.loads((ROOT/'release-manifest.json').read_text(encoding='utf-8'))
        for stage,record in manifest['prompt_interface_additions'].items():
            with self.subTest(stage=stage):
                final=(SKILL/'references/prompts'/f'{stage}.txt').read_bytes()
                prefix=final[:record['original_bytes']]
                self.assertEqual(hashlib.sha256(prefix).hexdigest(),record['original_sha256'])
                suffix=final[len(prefix):].decode('utf-8')
                self.assertTrue(suffix.startswith('\n\nLANGUAGE INTERFACE ADDITION\n'))
                self.assertEqual('DOCUMENT INTERFACE ADDITION' in suffix,stage in ('body','future'))
    def test_every_stage_has_unicode_language_interface(self):
        for stage in ('review','probabilities','selection','body','future'):
            text=(SKILL/'references/prompts'/f'{stage}.txt').read_text(encoding='utf-8')
            self.assertIn('Chinese and English',text)
            self.assertIn('valid Unicode JSON',text)
if __name__=='__main__':unittest.main()
