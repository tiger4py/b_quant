"""批量转换：目录下所有"八字命理分析*.md" → .docx"""
import sys
from pathlib import Path
from _md2docx import build_docx_from_md

BASE = Path(__file__).resolve().parent
for md_path in sorted(BASE.glob("八字命理分析*.md")):
    docx_path = md_path.with_suffix(".docx")
    print(f"转换: {md_path.name} → {docx_path.name}")
    build_docx_from_md(str(md_path), str(docx_path))
print("全部完成！")
