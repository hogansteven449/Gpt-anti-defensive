"""Local adapter contracts; binary smoke tests are opt-in for rendered QA."""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from readeraware import outputs
from readeraware.util import ValidationError

TEX = "\\documentclass{article}\n\\usepackage{amsmath}\n\\begin{document}\nFrozen α body. $a_b^2+\\frac{1}{2}$.\n\\end{document}\n% tail\n"
FUTURE = "\\section*{Future work}\nInvestigate $\\beta$ next."


class LatexAssemblyTests(unittest.TestCase):
    def test_future_goes_inside_document_and_body_reconstructs_exactly(self):
        result = outputs.assemble_latex(TEX, FUTURE)
        info = outputs.validate_latex_document(TEX)
        offset = info["end_document_start"]
        inserted = "\n\n" + FUTURE + "\n"
        self.assertEqual(result, TEX[:offset] + inserted + TEX[offset:])
        self.assertEqual(result[:offset] + result[offset + len(inserted):], TEX)
        self.assertIn(r"$a_b^2+\frac{1}{2}$", result)
        self.assertLess(result.index(FUTURE), result.index(r"\end{document}"))

    def test_empty_future_returns_identical_string(self):
        self.assertEqual(outputs.assemble_latex(TEX, ""), TEX)

    def test_comments_and_verbatim_do_not_create_document_boundaries(self):
        main = ("% \\documentclass{spoof} \\begin{document} \\end{document}\n"
                "\\documentclass{article}\n\\begin{document}\n"
                "\\verb|\\end{document}|\n"
                "\\begin{verbatim}\n\\documentclass{bad}\n\\end{document}\n\\end{verbatim}\n"
                "Literal \\% remains visible.\n\\end{document}\n")
        result = outputs.assemble_latex(main, "Appendix text")
        self.assertIn("Appendix text\n\\end{document}", result)
        self.assertEqual(outputs.validate_latex_document(result)["body_end"], result.rindex(r"\end{document}"))

    def test_structural_failures_are_rejected(self):
        for main in (TEX + "\\end{document}", TEX.replace("\\end{document}", ""),
                     TEX.replace("\\begin{document}", ""), TEX + "invisible prose",
                     TEX.replace("\\documentclass{article}", "\\documentclass{a}\\documentclass{b}")):
            with self.subTest(main=main), self.assertRaises(ValidationError):
                outputs.validate_latex_document(main)

    def test_future_cannot_close_document_or_change_preamble(self):
        for future in (r"\end{document}", r"\begin{document}oops", r"\documentclass{article}", r"\usepackage{foo}"):
            with self.subTest(future=future), self.assertRaises(ValidationError):
                outputs.assemble_latex(TEX, future)

    def test_unsafe_tex_is_rejected_even_without_compilation(self):
        for command in (r"\write18{touch x}", r"\input{../secret}", r"\input{|curl example.com}",
                        r"\directlua{os.execute('x')}", r"\csname write\endcsname", r"^^5cwrite18{x}",
                        r"\openout1=x", r"\input secret", r"\includegraphics{https://example.com/a.png}"):
            with self.subTest(command=command), self.assertRaises(ValidationError):
                outputs.validate_latex_fragment(command)
        self.assertTrue(outputs.validate_latex_fragment("% \\write18{comment}\n\\verb|\\write18{shown}| text")["valid"])


class MarkdownConversionTests(unittest.TestCase):
    def test_headings_lists_tables_code_and_math_have_real_tex_structure(self):
        markdown = ("# A & B\n\n**Bold** and `code_1` and $x_i^2$; 40% done.\n\n"
                    "- First\n- Second\n\n| Label | Value |\n| --- | --- |\n| alpha | 7 |\n\n"
                    "```text\nliteral \\command _ &\n```\n")
        result = outputs.markdown_to_latex(markdown)
        self.assertIn(r"\section*{A \& B}", result)
        self.assertIn(r"\textbf{Bold}", result)
        self.assertIn(r"\texttt{code\_1}", result)
        self.assertIn(r"\(x_i^2\)", result)
        self.assertIn(r"40\%", result)
        self.assertIn(r"\begin{itemize}", result)
        self.assertIn(r"\begin{longtable}", result)
        self.assertIn("literal \\command _ &", result)
        self.assertTrue(outputs.validate_latex_document(result)["valid"])

    def test_code_fence_and_unsupported_images_fail_explicitly(self):
        for markdown in ("```\nunclosed", "![plot](https://example.com/a.png)"):
            with self.subTest(markdown=markdown), self.assertRaises(ValidationError):
                outputs.markdown_to_latex(markdown)

    def test_math_detection_covers_tables_and_backslash_delimiters(self):
        self.assertTrue(outputs._has_math(r"Inline \(x+y\)"))
        self.assertTrue(outputs._has_math("| A | B |\n| --- | --- |\n| $x$ | 2 |"))
        self.assertFalse(outputs._has_math("`$not_math$`"))
        self.assertFalse(outputs._has_math("```\n$not_math$\n```"))

    def test_chinese_latex_wrapper_keeps_text_and_selects_editable_cjk_font(self):
        result = outputs.markdown_to_latex("# 中文标题\n\n中文正文和 $x_i^2$，引文 [1]。")
        self.assertIn("中文正文和", result)
        self.assertIn(r"\setmainfont{SimSun}", result)
        self.assertIn(r"\XeTeXlinebreaklocale", result)
        self.assertIn(r"\(x_i^2\)", result)
        self.assertNotIn("screenshot", result)

    def test_advanced_citations_are_not_silently_printed_as_unresolved_keys(self):
        self.assertTrue(outputs._has_structured_citations("Prior work [@sample2026]."))
        self.assertFalse(outputs._has_structured_citations("Literal `[@sample2026]` and visible [1]."))
        with self.assertRaisesRegex(ValidationError, "Resolve structured"):
            outputs.markdown_to_latex("Prior work [@sample2026].")

    def test_semantic_html_and_pandoc_table_syntax_require_structural_conversion(self):
        tables = [
            '<table><tr><th colspan="2">Merged heading</th></tr><tr><td>A</td><td>B</td></tr></table>',
            '+-----+-----+\n| A   | B   |\n+=====+=====+\n| one | two |\n+-----+-----+',
            'Header one   Header two\n----------   ----------\nCell one     Cell two',
        ]
        for table in tables:
            with self.subTest(table=table), self.assertRaisesRegex(ValidationError, "main-agent editable structural conversion"):
                outputs.markdown_to_latex(table)
            # Literal code examples and comments are not semantic tables.
            outputs._guard_bounded_markdown_tables("```text\n" + table + "\n```")
            outputs._guard_bounded_markdown_tables("<!--\n" + table + "\n-->\nVisible prose.")


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_markdown_original_bytes_and_prefix_are_preserved(self):
        main = "# Main\r\n\r\nFrozen α text.\r\n"
        future = "## Future\n\nNext steps.\n"
        report = outputs.export_outputs(self.root / "out", main, future, compiler="off")
        self.assertEqual(Path(report["source_artifacts"]["main"]).read_bytes(), main.encode("utf-8"))
        combined = Path(report["artifacts"]["markdown"]["combined"]).read_bytes()
        self.assertEqual(combined, (main + "\n\n" + future).encode("utf-8"))
        self.assertTrue(report["checks"]["final_prefix_verified"])
        self.assertEqual(report["checks"]["frozen_main_sha256_utf8"], hashlib.sha256(main.encode()).hexdigest())

    def test_plain_markdown_does_not_probe_or_launch_external_tools(self):
        with patch.object(outputs, "_find_pandoc", side_effect=AssertionError("unexpected pandoc lookup")), \
             patch.object(outputs, "_find_compiler", side_effect=AssertionError("unexpected TeX lookup")), \
             patch.object(outputs.subprocess, "run", side_effect=AssertionError("unexpected native process")):
            outputs.preflight_outputs(["markdown"], "markdown")
            outputs.export_outputs(self.root / "plain", "Frozen body", "Future")

    def test_latex_preserves_raw_main_and_reports_reconstruction(self):
        report = outputs.export_outputs(self.root / "out", TEX, FUTURE,
                                        content_format="latex", output_formats=["latex"], compiler="off")
        self.assertEqual(Path(report["source_artifacts"]["main"]).read_bytes(), TEX.encode("utf-8"))
        self.assertEqual(Path(report["artifacts"]["latex"]["combined"]).read_text(encoding="utf-8"), outputs.assemble_latex(TEX, FUTURE))
        self.assertTrue(report["checks"]["frozen_body_preserved"])
        self.assertTrue(report["checks"]["latex"]["frozen_body_reconstruction_verified"])
        self.assertFalse(report["checks"]["final_prefix_verified"])
        self.assertFalse(report["checks"]["latex"]["compiled"])

    def test_chinese_future_selects_cjk_wrapper_without_rewriting_english_body(self):
        main, future = "# English body\n\nFrozen prose.", "## 后续研究\n\n保持正文不变。"
        report = outputs.export_outputs(self.root / "mixed", main, future, output_formats=["latex"], compiler="off")
        converted = Path(report["artifacts"]["latex"]["combined"]).read_text(encoding="utf-8")
        self.assertIn(r"\setmainfont{SimSun}", converted)
        self.assertIn("Frozen prose.", converted)
        self.assertIn("保持正文不变。", converted)
        self.assertEqual(Path(report["source_artifacts"]["main"]).read_bytes(), main.encode("utf-8"))

    def test_unsupported_semantic_tables_cannot_report_successful_binary_export(self):
        html_table = '<table><tr><td rowspan="2">Merged</td><td>A</td></tr><tr><td>B</td></tr></table>'
        grid_table = '+-----+-----+\n| A   | B   |\n+=====+=====+\n| one | two |\n+-----+-----+'
        for table in (html_table, grid_table):
            for fmt in ("latex", "docx", "pdf"):
                with self.subTest(table=table, fmt=fmt), patch.object(outputs, "preflight_outputs", side_effect=AssertionError("guard must run before tools")):
                    with self.assertRaisesRegex(ValidationError, "main-agent editable structural conversion"):
                        outputs.export_outputs(self.root / (fmt + str(len(table))), "Frozen body.", table, output_formats=[fmt], compiler="off")
        report = outputs.export_outputs(self.root / "html-source", html_table, "", output_formats=["markdown"], compiler="off")
        self.assertEqual(Path(report["artifacts"]["markdown"]["combined"]).read_bytes(), html_table.encode("utf-8"))

    def test_missing_dependencies_fail_before_binary_conversion(self):
        with patch.object(outputs, "_find_pandoc", return_value=None), patch.object(outputs, "_find_compiler", return_value=None):
            with self.assertRaisesRegex(ValidationError, "Pandoc"):
                outputs.preflight_outputs(["docx"], "latex")
            with self.assertRaisesRegex(ValidationError, "local xelatex"):
                outputs.preflight_outputs(["pdf"], "latex")
            with self.assertRaisesRegex(ValidationError, "Mathematical DOCX"):
                outputs.export_outputs(self.root / "math", "$x^2$", "", output_formats=["docx"])
        self.assertFalse((self.root / "math/document.docx").exists())

    def test_assets_keep_relative_paths_and_collision_is_rejected(self):
        project = self.root / "source"
        project.mkdir()
        (project / "figures").mkdir()
        asset = project / "figures/plot.png"
        asset.write_bytes(b"synthetic local asset")
        provenance = {"sources": [{"source_id": "S001", "project_root": str(project)}],
                      "files": [{"source_id": "S001", "path": str(asset), "role": "tex-graphic", "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()}]}
        report = outputs.export_outputs(self.root / "out", TEX, "", content_format="latex",
                                        compiler="off", provenance=provenance)
        self.assertEqual((self.root / "out/figures/plot.png").read_bytes(), asset.read_bytes())
        self.assertEqual(report["assets"][0]["relative_path"], "figures/plot.png")
        (self.root / "out/figures/plot.png").write_bytes(b"different")
        with self.assertRaisesRegex(ValidationError, "Conflicting"):
            outputs.export_outputs(self.root / "out", TEX, "", content_format="latex", compiler="off", provenance=provenance)

    def test_snapshotted_asset_uses_recorded_project_relative_path(self):
        snapshot = self.root / "originals/S001/figures/plot.png"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(b"captured bytes")
        provenance = {"sources": [{"source_id": "S001", "project_root": str(self.root / "unavailable-original")}],
                      "files": [{"source_id": "S001", "path": str(snapshot),
                                 "original_path": str(self.root / "unavailable-original/figures/plot.png"),
                                 "relative_path": "S001/figures/plot.png", "role": "tex-graphic",
                                 "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()}]}
        report = outputs.export_outputs(self.root / "snap-export", TEX, "", content_format="latex",
                                        compiler="off", provenance=provenance)
        self.assertEqual((self.root / "snap-export/figures/plot.png").read_bytes(), b"captured bytes")
        self.assertEqual(report["assets"][0]["relative_path"], "figures/plot.png")

    def test_single_supplied_bbl_keeps_original_and_generated_job_alias(self):
        project = self.root / "project"
        project.mkdir()
        supplied = project / "paper.bbl"
        supplied.write_text(r"\begin{thebibliography}{1}\bibitem{sample}A supplied entry.\end{thebibliography}", encoding="utf-8")
        provenance = {"sources": [{"source_id": "S001", "project_root": str(project)}],
                      "files": [{"source_id": "S001", "path": str(supplied), "relative_path": "S001/paper.bbl",
                                 "role": "tex-bibliography-output", "sha256": hashlib.sha256(supplied.read_bytes()).hexdigest()}]}
        report = outputs.export_outputs(self.root / "bbl-export", TEX, "", content_format="latex", compiler="off", provenance=provenance)
        self.assertEqual((self.root / "bbl-export/document.bbl").read_bytes(), supplied.read_bytes())
        self.assertEqual((self.root / "bbl-export/paper.bbl").read_bytes(), supplied.read_bytes())

    def test_untrusted_compiler_wrappers_are_rejected(self):
        with patch.object(outputs.shutil, "which", return_value="/usr/bin/bash"):
            with self.assertRaisesRegex(ValidationError, "wrappers"):
                outputs.preflight_outputs(["latex"], "latex", compiler="bash")


class BibliographyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "document.tex"
        self.path.write_text(TEX, encoding="utf-8")
        (self.root / "references.bib").write_text("@book{sample,title={Local source},year={2026}}", encoding="utf-8")

    def test_bibtex_uses_argument_list_and_disables_miktex_installer(self):
        self.path.with_suffix(".aux").write_text(r"\bibdata{references}", encoding="utf-8")
        def run(command, cwd, **kwargs):
            self.assertEqual(command, ["local-bibtex", "--disable-installer", "document"])
            self.path.with_suffix(".bbl").write_text(r"\begin{thebibliography}{1}\bibitem{sample}Local source.\end{thebibliography}", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "Bibliography complete", "")
        with patch.object(outputs, "_bibliography_tool", return_value="local-bibtex"), patch.object(outputs, "_run", side_effect=run):
            metadata, log = outputs._run_bibliography(self.path, "xelatex", {}, True, 0)
        self.assertEqual(metadata["tool"], "bibtex")
        self.assertFalse(metadata["remote_data_sources"])

    def test_biber_validates_local_control_sources_and_ignores_configs(self):
        self.path.with_suffix(".bcf").write_text('<control><datasource type="file" datatype="bibtex">references.bib</datasource></control>', encoding="utf-8")
        def run(command, cwd, **kwargs):
            self.assertEqual(command, ["local-biber", "--noconf", "document"])
            self.path.with_suffix(".bbl").write_text(r"\entry{sample}{book}{}", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "Biber complete", "")
        with patch.object(outputs, "_bibliography_tool", return_value="local-biber"), patch.object(outputs, "_run", side_effect=run):
            metadata, _ = outputs._run_bibliography(self.path, "xelatex", {}, True, 0)
        self.assertEqual(metadata["tool"], "biber")
        self.path.with_suffix(".bcf").write_text('<control><datasource type="file">https://example.com/remote.bib</datasource></control>', encoding="utf-8")
        with patch.object(outputs, "_bibliography_tool", return_value="local-biber"), self.assertRaisesRegex(ValidationError, "literal local"):
            outputs._run_bibliography(self.path, "xelatex", {}, True, 0)

    def test_preprovided_bbl_is_retained_when_tool_absent(self):
        self.path.with_suffix(".aux").write_text(r"\bibdata{references}", encoding="utf-8")
        with patch.object(outputs, "_bibliography_tool", return_value=None), self.assertRaisesRegex(ValidationError, "preprovided"):
            outputs._run_bibliography(self.path, "xelatex", {}, True, 0)
        bbl = r"\begin{thebibliography}{1}\bibitem{sample}Supplied entry.\end{thebibliography}"
        self.path.with_suffix(".bbl").write_text(bbl, encoding="utf-8")
        with patch.object(outputs, "_bibliography_tool", return_value=None):
            metadata, _ = outputs._run_bibliography(self.path, "xelatex", {}, True, 0)
        self.assertEqual(metadata["tool"], "preprovided-bbl")
        self.assertEqual(self.path.with_suffix(".bbl").read_text(encoding="utf-8"), bbl)


@unittest.skipUnless(os.environ.get("READERAWARE_RUN_BINARY_EXPORT_TESTS") == "1",
                     "Opt in after the artifact marker; root performs render/visual QA")
class BinaryExportSmokeTests(unittest.TestCase):
    def test_docx_and_pdf_have_complete_text(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            report = outputs.export_outputs(directory, "# Adapter verification\n\nThe α finding is clear.\n\n| Item | Count |\n| --- | --- |\n| Sample | 7 |\n", "## Future work\n\nExplore the next question.",
                                            output_formats=["markdown", "docx", "pdf"], compiler="off")
            for kind in ("docx", "pdf"):
                self.assertTrue(Path(report["artifacts"][kind]["combined"]).is_file())
                self.assertTrue(report["checks"]["formats"][kind]["full_text_verified"])

    def test_local_images_embed_and_native_word_tables_keep_shape(self):
        from PIL import Image
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            image = root / "source.png"
            Image.new("RGB", (160, 90), (42, 93, 148)).save(image)
            provenance = {"sources": [], "files": [{"source_id": "S001", "path": str(image),
                "relative_path": "S001/source.png", "output_relative_path": "resources/S001/figure.png",
                "role": "docx-image", "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}]}
            main = "# Rich adapter verification\n\nFrozen prose before the image.\n\n![A local figure](resources/S001/figure.png)\n\n| Item | Count |\n| --- | --- |\n| Sample | 7 |\n| Follow up | 12 |\n"
            report = outputs.export_outputs(root / "out", main, "## Future work\n\nNext question.",
                                            output_formats=["markdown", "latex", "docx", "pdf"],
                                            provenance=provenance, compiler="off")
            docx_check = report["checks"]["formats"]["docx"]
            self.assertEqual(docx_check["table_shapes"], [{"rows": 3, "columns": 2}])
            self.assertEqual(docx_check["image_occurrences"], 1)
            self.assertTrue(docx_check["image_bytes_preserved"])
            self.assertTrue(report["checks"]["formats"]["pdf"]["decoded_image_pixels_preserved"])
            wrong_structure = {"images": [str(root / "out/resources/S001/figure.png")], "tables": [{"rows": 2, "columns": 2}]}
            with self.assertRaisesRegex(ValidationError, "table row/column"):
                outputs._docx_structure_check(Path(report["artifacts"]["docx"]["combined"]), wrong_structure)

    def test_remote_and_escaping_image_references_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            for target in ("https://example.com/image.png", "../outside.png", "%2e%2e/outside.png", "C:/private.png"):
                with self.subTest(target=target), self.assertRaises(ValidationError):
                    outputs._image_path(target, Path(directory))

    def test_chinese_docx_reopen_and_pdf_glyph_text_roundtrip(self):
        from PIL import Image
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            image = root / "figure.png"
            Image.new("RGB", (120, 70), (43, 99, 155)).save(image)
            main = "# 中文验证\n\n中文正文、原生表格和编号引文 [1]。\n\n| 样本 | 数量 |\n| --- | --- |\n| 甲组 | 7 |\n\n![样本图](figure.png)\n\n## 参考文献\n\n[1] 张明。格式适配。2026。\n"
            report = outputs.export_outputs(root, main, "## 后续研究\n\n继续研究。", output_formats=["docx", "pdf"], compiler="off")
            self.assertTrue(report["checks"]["formats"]["docx"]["cjk_text_roundtrip_verified"])
            self.assertTrue(report["checks"]["formats"]["docx"]["reopened_roundtrip_verified"])
            self.assertTrue(report["checks"]["formats"]["pdf"]["full_text_verified"])
            self.assertTrue(report["checks"]["formats"]["pdf"]["decoded_image_pixels_preserved"])

    def test_chinese_math_stays_editable_in_pandoc_docx(self):
        if not outputs._find_pandoc(): self.skipTest("Local Pandoc is unavailable")
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            report = outputs.export_outputs(directory, "# 中文公式\n\n中文正文和 $x_i^2+\\frac{1}{2}$，保留引文 [1]。\n\n| 样本 | 数量 |\n| --- | --- |\n| 甲组 | 7 |\n", "", output_formats=["docx"], compiler="off")
            check = report["checks"]["formats"]["docx"]
            self.assertEqual(check["exported_math_count"], 1)
            self.assertEqual(check["editable_math_roundtrip_count"], 1)
            self.assertTrue(check["cjk_text_roundtrip_verified"])
            self.assertEqual(check["table_shapes"], [{"rows": 2, "columns": 2}])


if __name__ == "__main__":
    unittest.main()
