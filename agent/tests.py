import os
import pandas as pd
from django.conf import settings
from django.test import TestCase
from agent.services.sandbox import run_pandas_snippet, _validate_ast, _auto_assign_answer
from agent.services.excel_parser import parse_workbook, build_batch, _profile_dataframe

class ExcelAgentTestSuite(TestCase):
    def test_basic_snippet(self):
        df = pd.DataFrame({"a": [10, 20, 30], "b": [1, 2, 3]})
        code = "answer = df['a'].sum()"
        status, res = run_pandas_snippet(code, {"df": df})
        self.assertEqual(status, "ok")
        self.assertEqual(res, 60)

    def test_auto_assign_bare_expression(self):
        df = pd.DataFrame({"a": [5, 15, 25]})
        code = "df['a'].max()"
        status, res = run_pandas_snippet(code, {"df": df})
        self.assertEqual(status, "ok")
        self.assertEqual(res, 25)

    def test_safe_imports(self):
        df = pd.DataFrame({"a": [4, 9, 16]})
        code = """
import numpy as np
import math
answer = [math.sqrt(x) for x in df['a']]
"""
        status, res = run_pandas_snippet(code, {"df": df})
        self.assertEqual(status, "ok")
        self.assertEqual(res, [2.0, 3.0, 4.0])

    def test_ifexp_ternary(self):
        df = pd.DataFrame({"val": [10, 20]})
        code = "answer = 'high' if df['val'].max() > 15 else 'low'"
        status, res = run_pandas_snippet(code, {"df": df})
        self.assertEqual(status, "ok")
        self.assertEqual(res, "high")

    def test_profiling_and_schema(self):
        df = pd.DataFrame({
            "Branch": [" ADIBATLA ", "DILSUKNAGAR", "KHALSA"],
            "Bldg Type": ["Non-AC", "AC", "Non-AC"],
            "Score": [85, 92, 78],
        })
        # Test whitespace cleanup
        df["Branch"] = df["Branch"].apply(lambda x: x.strip() if isinstance(x, str) else x)
        self.assertEqual(df["Branch"].iloc[0], "ADIBATLA")

        profiles = _profile_dataframe(df)
        self.assertEqual(profiles["Bldg Type"]["kind"], "category")
        self.assertIn("Non-AC", profiles["Bldg Type"]["values"])
        self.assertEqual(profiles["Score"]["kind"], "numeric")
        self.assertEqual(profiles["Score"]["min"], 78)
        self.assertEqual(profiles["Score"]["max"], 92)

    def test_parse_sample_file(self):
        sample_path = settings.BASE_DIR / "media" / "workbooks" / "sample_marks_ranks_demo.xlsx"
        if sample_path.exists():
            parsed = parse_workbook(str(sample_path))
            self.assertIn("Marks Ranks", parsed.sheets)
            sheet = parsed.sheets["Marks Ranks"]
            self.assertEqual(sheet.header_row_index, 2)
            self.assertGreaterEqual(sheet.n_cols, 50)
            self.assertEqual(sheet.n_rows, 15)

    def test_cleanup_expired_session(self):
        from datetime import timedelta
        from django.utils import timezone
        from agent.models import Project
        from agent.services.cleanup import cleanup_expired_sessions, cleanup_project, get_storage_stats
        from pathlib import Path

        # Create a test project
        project = Project.objects.create()
        # Manually backdate created_at to 15 hours ago
        Project.objects.filter(id=project.id).update(
            created_at=timezone.now() - timedelta(hours=15)
        )

        # Create dummy parsed pickle file
        parsed_dir = Path(settings.MEDIA_ROOT) / "parsed"
        parsed_dir.mkdir(exist_ok=True)
        test_pkl = parsed_dir / f"{project.id}.pkl"
        test_pkl.write_bytes(b"dummy pickle content")
        self.assertTrue(test_pkl.exists())

        # Run cleanup with 12 hour threshold
        result = cleanup_expired_sessions(retention_hours=12)
        self.assertGreaterEqual(result["projects_deleted"], 1)
        self.assertFalse(Project.objects.filter(id=project.id).exists())
        self.assertFalse(test_pkl.exists())

    def test_cleanup_recent_session_preserved(self):
        from agent.models import Project
        from agent.services.cleanup import cleanup_expired_sessions
        from pathlib import Path

        # Fresh project (now)
        fresh_project = Project.objects.create()
        parsed_dir = Path(settings.MEDIA_ROOT) / "parsed"
        parsed_dir.mkdir(exist_ok=True)
        test_pkl = parsed_dir / f"{fresh_project.id}.pkl"
        test_pkl.write_bytes(b"dummy fresh content")

        try:
            cleanup_expired_sessions(retention_hours=12)
            # Must still exist because it was created now (< 12 hours)
            self.assertTrue(Project.objects.filter(id=fresh_project.id).exists())
            self.assertTrue(test_pkl.exists())
        finally:
            fresh_project.delete()
            test_pkl.unlink(missing_ok=True)

    def test_storage_stats(self):
        from agent.services.cleanup import get_storage_stats
        stats = get_storage_stats()
        self.assertIn("total_files", stats)
        self.assertIn("total_size_mb", stats)
        self.assertIn("categories", stats)
        self.assertIn("workbooks", stats["categories"])

