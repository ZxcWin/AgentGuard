from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from xml.sax.saxutils import escape


OUT = Path("九年级数学上册新教材解读课件_30分钟精简对比版.pptx")
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def esc(value: str) -> str:
    return escape(value, {'"': '&quot;'})


def tx_body(lines: list[str], size: int = 1900, color: str = "203040", bold_first: bool = False) -> str:
    paragraphs = []
    for index, line in enumerate(lines):
        bold = "<a:b/>" if bold_first and index == 0 else ""
        paragraphs.append(
            f'<a:p><a:pPr marL="0" indent="0"/><a:r><a:rPr lang="zh-CN" sz="{size}">{bold}'
            f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
            f'<a:latin typeface="Microsoft YaHei"/><a:ea typeface="Microsoft YaHei"/></a:rPr>'
            f'<a:t>{esc(line)}</a:t></a:r><a:endParaRPr lang="zh-CN"/></a:p>'
        )
    return "".join(paragraphs)


def shape_text(shape_id: int, x: int, y: int, w: int, h: int, lines: list[str], *, size: int = 1900, color: str = "203040", bold_first: bool = False, fill: str | None = None) -> str:
    fill_xml = f'<p:spPr><a:solidFill><a:srgbClr val="{fill}"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr>' if fill else '<p:spPr><a:noFill/></p:spPr>'
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="文本框 {shape_id}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm></p:spPr>'
        f'<p:txBody><a:bodyPr wrap="square" rtlCol="0" anchor="t"/><a:lstStyle/>{tx_body(lines, size, color, bold_first)}</p:txBody></p:sp>'
    )


def rect(shape_id: int, x: int, y: int, w: int, h: int, fill: str) -> str:
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="色块 {shape_id}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
        f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr></p:sp>'
    )


def slide_xml(title: str, subtitle: str, blocks: list[tuple[int, int, int, int, list[str], int, str, bool, str | None]], number: int) -> str:
    shapes = [rect(2, 0, 0, 12192000, 685800, "1769AA")]
    shapes.append(shape_text(3, 450000, 130000, 11200000, 380000, [title], size=2600, color="FFFFFF", bold_first=True))
    if subtitle:
        shapes.append(shape_text(4, 520000, 800000, 11000000, 350000, [subtitle], size=1250, color="527080"))
    sid = 10
    for x, y, w, h, lines, size, color, bold_first, fill in blocks:
        if fill:
            shapes.append(rect(sid, x - 70000, y - 60000, w + 140000, h + 100000, fill))
            sid += 1
        shapes.append(shape_text(sid, x, y, w, h, lines, size=size, color=color, bold_first=bold_first))
        sid += 1
    shapes.append(shape_text(90, 10800000, 6500000, 900000, 250000, [f"{number:02d}"], size=1100, color="7A8E99"))
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="{NS_A}" xmlns:r="{NS_R}" xmlns:p="{NS_P}"><p:cSld><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
{''.join(shapes)}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'''


def blank_layout() -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="{NS_A}" xmlns:r="{NS_R}" xmlns:p="{NS_P}" type="blank"><p:cSld name="空白"><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr></p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>'''


def write_deck() -> None:
    slides = [
        ("九年级数学上册新教材解读", "人教版｜30 分钟精简对比版（建议讲解：16 页）", [(800000, 1900000, 10600000, 800000, ["从“目录变化”讲到“课堂怎么教”"], 3000, "1769AA", True, None), (800000, 3200000, 10000000, 700000, ["核心方法：少讲逐页内容，多讲结构变化、学习路径和教学落点"], 1900, "203040", False, "EAF4F8")]),
        ("30 分钟讲解路线", "建议不要逐页照读，按“变化—原因—教法”推进", [(700000, 1400000, 5200000, 2300000, ["01 目录重组｜5 分钟", "旧版 5 章 → 新版 6 章", "先建立全册地图"], 1800, "203040", True, "EAF4F8"), (6500000, 1400000, 5200000, 2300000, ["02 内容变化｜10 分钟", "函数、圆、概率的编排变化", "抓住 4 个关键对比"], 1800, "203040", True, "F5F1E6"), (700000, 4200000, 5200000, 1800000, ["03 章节解读｜10 分钟", "每章只讲：新增/调整、难点、教法"], 1800, "203040", True, "EAF4F8"), (6500000, 4200000, 5200000, 1800000, ["04 教学收束｜5 分钟", "从“讲知识”转向“建结构、做探究、重表达”"], 1800, "203040", True, "F5F1E6")]),
        ("先给结论：新版不是简单“加内容”", "最值得讲清楚的是编排逻辑的变化", [(800000, 1450000, 10400000, 850000, ["旧版：章节相对分散，部分内容混排，教师容易按课时切碎。"], 2200, "203040", False, "F5F1E6"), (800000, 2700000, 10400000, 850000, ["新版：函数链更完整，圆的专题边界更清楚，活动与信息技术应用更显性。"], 2200, "203040", False, "EAF4F8"), (800000, 4050000, 10400000, 1050000, ["教学变化：从“把知识讲完”转为“让学生经历产生、联系、应用和表达”。"], 2200, "1769AA", True, "EAF4F8")]),
        ("总览对比：2014 版九上 vs 新版九上", "用一张表讲清全册结构变化", [(700000, 1350000, 3300000, 400000, ["对比维度"], 1700, "FFFFFF", True, "1769AA"), (4000000, 1350000, 3300000, 400000, ["2014 版"], 1700, "FFFFFF", True, "1769AA"), (7300000, 1350000, 4000000, 400000, ["新版（课件标注存在 2024/2026 两种说法，需核对印刷版）"], 1450, "FFFFFF", True, "1769AA"), (700000, 2000000, 3300000, 2800000, ["章节总数", "函数板块", "圆的编排", "教学载体"], 1800, "203040", True, "EAF4F8"), (4000000, 2000000, 3300000, 2800000, ["5 章", "二次函数在九上；反比例函数在九下", "圆单章，内容较集中", "阅读与思考、实验与探究"], 1650, "203040", False, "FFFFFF"), (7300000, 2000000, 4000000, 2800000, ["6 章", "一元二次方程 → 二次函数 → 反比例函数集中在上册", "圆基础与直线/圆专题拆分", "数学活动、信息技术应用更突出"], 1650, "203040", False, "FFFFFF")]),
        ("变化一：函数知识链前移并闭环", "这是新版最容易被学生感知的结构变化", [(700000, 1500000, 4800000, 2300000, ["2014 版", "九上重点是二次函数", "反比例函数放在九下", "函数知识被学期切开"], 1900, "203040", True, "F5F1E6"), (6700000, 1500000, 4800000, 2300000, ["新版", "一元二次方程 → 二次函数 → 反比例函数", "同一学期形成函数认知递进", "更适合围绕图象、关系、模型串联复习"], 1900, "203040", True, "EAF4F8"), (700000, 4400000, 10400000, 1000000, ["细节对比：旧版强调“各章会做题”；新版更强调“不同函数之间有什么关系、如何用模型解释问题”。"], 1900, "1769AA", True, None)]),
        ("变化二：圆的内容从“混排”到“专题化”", "把切线、内切圆、正多边形从章节边角变成可追踪的学习主线", [(700000, 1450000, 4800000, 2500000, ["2014 版：圆（单章）", "圆的性质、点与圆、直线与圆、正多边形、弧长扇形等放在一章", "优点：集中；难点：内容密度高、主线易散"], 1750, "203040", True, "F5F1E6"), (6700000, 1450000, 4800000, 2500000, ["新版：拆出两个专题", "圆的有关性质", "直线与圆的位置关系", "切线、内切圆、正多边形形成重点专题"], 1750, "203040", True, "EAF4F8"), (700000, 4400000, 10400000, 800000, ["教学提示：用“直线形—圆—综合模型”串联，而不是把每个定理孤立讲解。"], 1900, "1769AA", True, None)]),
        ("变化三：教材更强调核心素养的形成过程", "课件给出的关键词可压缩为“四个转向”", [(700000, 1450000, 2400000, 2400000, ["从结论", "→ 过程", "展示概念、定理、模型怎样产生"], 1800, "203040", True, "EAF4F8"), (3400000, 1450000, 2400000, 2400000, ["从例题", "→ 任务", "让学生经历观察、猜想、验证、表达"], 1800, "203040", True, "F5F1E6"), (6100000, 1450000, 2400000, 2400000, ["从单章", "→ 结构", "主动建立章节之间的联系"], 1800, "203040", True, "EAF4F8"), (8800000, 1450000, 2400000, 2400000, ["从做题", "→ 应用", "关注真实情境和数学建模"], 1800, "203040", True, "F5F1E6"), (700000, 4550000, 10400000, 700000, ["讲解时不要泛泛说“落实核心素养”，要落到：学生具体观察什么、推理什么、表达什么。"], 1800, "1769AA", True, None)]),
        ("新版九上章节地图：讲课时只保留 6 个关键词", "每章都用“核心对象—关键关系—典型应用”三句话带过", [(700000, 1400000, 10400000, 3000000, ["一元二次方程｜降次、根与实际问题", "二次函数｜图象性质、方程联系、最值模型", "旋转｜对应点、中心对称、图案设计", "圆｜圆心角、弧、垂径、切线基础", "概率初步｜随机事件、列举法、频率估计", "直线与圆｜相离/相切/相交、内切圆、正多边形"], 2000, "203040", False, "EAF4F8"), (700000, 4900000, 10400000, 850000, ["建议讲法：不按教材页码复述，而按这 6 个关键词建立“考点—思维—应用”地图。"], 1900, "1769AA", True, None)]),
        ("章节对比一：一元二次方程", "从“解法章节”转向“方程—模型—函数”的连接点", [(700000, 1450000, 4700000, 2400000, ["旧版关注", "概念、配方法、公式法、因式分解、实际问题", "课堂容易变成解题方法清单"], 1850, "203040", True, "F5F1E6"), (6700000, 1450000, 4700000, 2400000, ["新版建议抓住", "降次思想：把不会的问题转化为会的问题", "强调根的意义、方程与函数图象的联系", "应用题从“套公式”转向“设元—建模—检验”"], 1850, "203040", True, "EAF4F8"), (700000, 4400000, 10400000, 700000, ["对比细节：同样是解方程，旧版更像“方法分类”；新版更适合追问“为什么要降次、根代表什么”。"], 1800, "1769AA", True, None)]),
        ("章节对比二：二次函数与反比例函数", "把图象、性质和方程联系起来讲", [(700000, 1400000, 5000000, 2900000, ["二次函数", "核心：图象与性质", "关键连接：与一元二次方程的根、交点、最值相互解释", "应用：运动、面积、利润等模型"], 1850, "203040", True, "EAF4F8"), (6400000, 1400000, 5000000, 2900000, ["反比例函数（新版前移）", "核心：数量关系 y=k/x 与图象分支", "关键连接：正比例/一次/二次/反比例的变化特征", "应用：速度—时间、功率—电阻、面积关系"], 1850, "203040", True, "F5F1E6"), (700000, 4750000, 10400000, 700000, ["对比细节：新版不是增加一章而已，而是让学生在同一册中看到“不同函数如何描述不同变化”。"], 1800, "1769AA", True, None)]),
        ("章节对比三：旋转", "保留知识主干，强化几何变换与实践活动", [(700000, 1450000, 4700000, 2300000, ["知识主干", "图形的旋转、中心对称", "对应点、对应线段、对应角", "性质要通过作图和观察获得"], 1850, "203040", True, "EAF4F8"), (6700000, 1450000, 4700000, 2300000, ["教学加点", "把旋转与全等、对称、图案设计联系起来", "安排尺规/信息技术辅助探索", "从“记性质”转向“能识别变换并表达理由”"], 1850, "203040", True, "F5F1E6"), (700000, 4300000, 10400000, 800000, ["适合半小时点到为止：用一个图案设计案例，说明新版把“做数学”放得更前。"], 1800, "1769AA", True, None)]),
        ("章节对比四：圆与直线与圆", "新版最明显的几何专题化调整", [(700000, 1400000, 4800000, 2700000, ["圆的有关性质", "圆周角、垂径定理、弧与弦等基础结构", "教学重点：定理之间的联系和证明表达"], 1850, "203040", True, "EAF4F8"), (6700000, 1400000, 4800000, 2700000, ["直线与圆的位置关系", "相离、相切、相交", "切线判定与性质、切线长", "三角形内切圆、正多边形与圆"], 1850, "203040", True, "F5F1E6"), (700000, 4550000, 10400000, 850000, ["对比细节：切线不再只是“圆章中的一节”，而成为连接直线形与曲线形的综合桥梁。"], 1800, "1769AA", True, None)]),
        ("章节对比五：概率初步", "从公式记忆走向“随机—频率—概率”的认识路径", [(700000, 1450000, 4700000, 2300000, ["基础内容", "随机事件与概率", "列举法求概率", "频率估计概率"], 1900, "203040", True, "EAF4F8"), (6700000, 1450000, 4700000, 2300000, ["教学细节", "用实验数据呈现频率的稳定趋势", "区分等可能与非等可能情形", "把“概率与中奖”等阅读材料作为辨析机会"], 1900, "203040", True, "F5F1E6"), (700000, 4300000, 10400000, 800000, ["对比重点：不只问“算出多少”，还要问“这个数怎样产生、在什么条件下可信”。"], 1800, "1769AA", True, None)]),
        ("教学安排：半小时解读可以这样落地", "把 100 多页内容压缩成 5 个教学动作", [(700000, 1400000, 10400000, 3600000, ["动作 1｜先画全册结构图：6 章分别解决什么问题", "动作 2｜重点讲 4 个对比：函数前移、圆的拆分、活动增强、过程性学习", "动作 3｜每章只留一个“核心模型”或“核心关系”", "动作 4｜补一句课堂变化：从讲结论到组织观察、猜想、验证、表达", "动作 5｜最后落到备课：章节之间要联通，活动要留下证据，评价要看思路表达"], 1900, "203040", False, "EAF4F8")]),
        ("旧版—新版：教师备课视角的细节对比", "这页适合用来和老师交流“到底变在哪里”", [(700000, 1350000, 3300000, 400000, ["备课环节"], 1750, "FFFFFF", True, "1769AA"), (4000000, 1350000, 3300000, 400000, ["旧版倾向"], 1750, "FFFFFF", True, "1769AA"), (7300000, 1350000, 4000000, 400000, ["新版倾向"], 1750, "FFFFFF", True, "1769AA"), (700000, 2000000, 3300000, 3000000, ["目标", "例题", "活动", "评价"], 1800, "203040", True, "EAF4F8"), (4000000, 2000000, 3300000, 3000000, ["知识点覆盖", "方法归类", "活动作为补充", "结果与正确率"], 1750, "203040", False, "FFFFFF"), (7300000, 2000000, 4000000, 3000000, ["结构与素养目标", "模型与联系", "活动进入学习过程", "过程、理由、表达与应用"], 1750, "203040", False, "FFFFFF")]),
        ("30 分钟讲解的推荐节奏", "每个模块只讲一个结论、一个例子、一个教学动作", [(700000, 1400000, 10400000, 3700000, ["0—3 分钟｜说明修订背景与今天的讲法", "3—8 分钟｜目录和全册结构对比", "8—15 分钟｜函数、圆、章节编排的三组变化", "15—25 分钟｜五类章节各讲一个核心变化与教学落点", "25—28 分钟｜备课视角的旧版—新版对比", "28—30 分钟｜总结：结构更清楚，联系更紧，过程更重要"], 2050, "203040", False, "F5F1E6"), (700000, 5350000, 10400000, 650000, ["控时原则：出现重复的目录页、逐条教材截图页、同义教学建议页时，直接跳过或合并。"], 1800, "1769AA", True, None)]),
        ("收束：听众最后只需记住 5 句话", "精简版结束页，可直接作为现场总结", [(800000, 1350000, 10400000, 3900000, ["1. 新版九上由 5 章调整为 6 章，结构重组是主线。", "2. 函数内容更集中，形成一元二次方程—二次函数—反比例函数的认知链。", "3. 圆的内容专题化，直线与圆的位置关系被单独凸显。", "4. 教材更重视探究、活动、信息技术应用和数学表达。", "5. 备课不能只按页推进，要围绕知识联系、核心模型和学习过程重新组织。"], 2200, "203040", False, "EAF4F8"), (800000, 5550000, 10400000, 500000, ["备注：原课件内部同时出现“2024 版”和“2026 版”表述，正式使用前请以实际教材封面、版权页和当地发行版本为准。"], 1450, "A34B35", False, "FFF1F0")]),
    ]
    slide_count = len(slides)
    presentation = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="{NS_A}" xmlns:r="{NS_R}" xmlns:p="{NS_P}"><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst><p:sldIdLst>{''.join(f'<p:sldId id="{255+i}" r:id="rId{i+2}"/>' for i in range(slide_count))}</p:sldIdLst><p:sldSz cx="12192000" cy="6858000" type="screen16x9"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>'''
    rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>']
    pres_rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>' + ''.join(f'<Relationship Id="rId{i+2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i+1}.xml"/>' for i in range(slide_count)) + '</Relationships>']
    content_types = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/><Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>' + ''.join(f'<Override PartName="/ppt/slides/slide{i+1}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>' for i in range(slide_count)) + '</Types>']
    master = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldMaster xmlns:a="{NS_A}" xmlns:r="{NS_R}" xmlns:p="{NS_P}"><p:cSld name="母版"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr></p:spTree></p:cSld><p:sldLayoutIdLst><p:sldLayoutId id="1" r:id="rId1"/></p:sldLayoutIdLst><p:txStyles/></p:sldMaster>'''
    master_rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>'
    layout_rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>'
    with ZipFile(OUT, 'w', ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', content_types[0])
        z.writestr('_rels/.rels', rels[0])
        z.writestr('ppt/presentation.xml', presentation)
        z.writestr('ppt/_rels/presentation.xml.rels', pres_rels[0])
        z.writestr('ppt/slideMasters/slideMaster1.xml', master)
        z.writestr('ppt/slideMasters/_rels/slideMaster1.xml.rels', master_rels)
        z.writestr('ppt/slideLayouts/slideLayout1.xml', blank_layout())
        z.writestr('ppt/slideLayouts/_rels/slideLayout1.xml.rels', layout_rels)
        for i, (title, subtitle, blocks) in enumerate(slides, 1):
            z.writestr(f'ppt/slides/slide{i}.xml', slide_xml(title, subtitle, blocks, i))
            z.writestr(f'ppt/slides/_rels/slide{i}.xml.rels', f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>')
        z.writestr('docProps/core.xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"><dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">九年级数学上册新教材解读课件——30分钟精简对比版</dc:title><dc:language xmlns:dc="http://purl.org/dc/elements/1.1/">zh-CN</dc:language></cp:coreProperties>')


if __name__ == "__main__":
    write_deck()
    print(OUT)
