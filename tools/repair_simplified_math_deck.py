from __future__ import annotations

from pathlib import Path
from shutil import copyfile
from zipfile import ZIP_DEFLATED, ZipFile
import xml.etree.ElementTree as ET


BASE = Path("九年级数学上册新教材解读课件（人教版）.pptx")
NEW = Path("九年级数学上册新教材解读课件_30分钟精简对比版.pptx")
TMP = Path("/tmp/精简版课件修复.pptx")

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
ET.register_namespace("p", P_NS)
ET.register_namespace("r", R_NS)


def repair() -> None:
    with ZipFile(BASE) as base, ZipFile(NEW) as generated:
        files = {name: base.read(name) for name in base.namelist()}
        generated_slides = {
            name: generated.read(name)
            for name in generated.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        }
    slide_count = len(generated_slides)

    presentation = ET.fromstring(files["ppt/presentation.xml"])
    slide_list = presentation.find(f"{{{P_NS}}}sldIdLst")
    if slide_list is None:
        raise RuntimeError("原课件缺少幻灯片列表")
    for child in list(slide_list):
        slide_list.remove(child)
    for index in range(1, slide_count + 1):
        ET.SubElement(slide_list, f"{{{P_NS}}}sldId", {"id": str(300 + index), f"{{{R_NS}}}id": f"rId{index + 10}"})
    files["ppt/presentation.xml"] = ET.tostring(presentation, encoding="utf-8", xml_declaration=True)

    rels = ET.fromstring(files["ppt/_rels/presentation.xml.rels"])
    for child in list(rels):
        if child.attrib.get("Type", "").endswith("/slide"):
            rels.remove(child)
    for index in range(1, slide_count + 1):
        ET.SubElement(rels, f"{{{PKG_NS}}}Relationship", {"Id": f"rId{index + 10}", "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide", "Target": f"slides/slide{index}.xml"})
    files["ppt/_rels/presentation.xml.rels"] = ET.tostring(rels, encoding="utf-8", xml_declaration=True)

    layout_rel = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>'''
    for index in range(1, slide_count + 1):
        files[f"ppt/slides/slide{index}.xml"] = generated_slides[f"ppt/slides/slide{index}.xml"]
        files[f"ppt/slides/_rels/slide{index}.xml.rels"] = layout_rel

    with ZipFile(TMP, "w", ZIP_DEFLATED) as output:
        for name, data in files.items():
            output.writestr(name, data)
    copyfile(TMP, NEW)
    TMP.unlink()


if __name__ == "__main__":
    repair()
    print(NEW)
