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
