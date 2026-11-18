"""字幕语言后缀识别 + 「规制解除 / 限制解除」算作无删减。

两条都来自生产：
- 2026-11-10 赛博朋克：边缘行者（VCB-Studio BDRip）每集带 `.JPSC.ass` / `.JPTC.ass`，
  语言标记认不出，简繁两份都要改成无后缀的 `S01E0x.ass`，繁体那份每轮「集位被占」跳过。
- 2026-11-09 学生会也有洞！悠哈璃羽「规制解除版」没拿到无删减 +100，合并发布里的
  「限制解除版」文件 0 分；判重能不能留下无修版全看别的偏好项碰巧够不够。
"""
import pytest

from media_agent import preferences
from media_agent.naming import subtitle_lang_tag, target_subtitle_filename

VCB = "[Nekomoe kissaten&VCB-Studio] Cyberpunk Edgerunners [01][Ma10p_1080p][x265_flac]"


@pytest.mark.parametrize("name, tag", [
    (f"{VCB}.JPSC.ass", "JPSC"),
    (f"{VCB}.JPTC.ass", "JPTC"),
    ("x.jpsc.ass", "jpsc"),
    ("x.scjp.ass", "scjp"),
    ("x.tcjp.ass", "tcjp"),
    ("x.sc.ass", "sc"),
    ("x.TC.ass", "TC"),
    ("x.chs.ass", "chs"),
    ("x.CHT.srt", "CHT"),
    ("x.big5.ass", "big5"),
    ("x.zh-CN.ass", "zh-CN"),
    ("x.zh-Hant.ass", "zh-Hant"),
])
def test_recognized(name, tag):
    assert subtitle_lang_tag(name) == tag


@pytest.mark.parametrize("name", [
    "学生会也有洞！ S01E01.ass",
    "[DBD-Raws][X][01][1080P][BDRip][HEVC-10bit][FLACx2].ass",
    "x.v2.ass",
])
def test_no_tag(name):
    assert subtitle_lang_tag(name) is None


def test_vcb_pair_gets_distinct_targets():
    """同一集的简繁两份必须落到两个不同的名字，否则必有一份永远改不过去。"""
    names = {target_subtitle_filename("赛博朋克：边缘行者", 1, 1,
                                      subtitle_lang_tag(f"{VCB}.{t}.ass"), ".ass")
             for t in ("JPSC", "JPTC")}
    assert names == {"赛博朋克：边缘行者 S01E01.JPSC.ass", "赛博朋克：边缘行者 S01E01.JPTC.ass"}


@pytest.mark.parametrize("uncut, tv", [
    ("【悠哈璃羽字幕社】[脑洞学生会 / 学生会也有洞 / Seitokai ni mo Ana wa Aru][规制解除版][05][1080p HEVC][CHS&CHT][简繁内封]",
     "【悠哈璃羽字幕社】[脑洞学生会 / 学生会也有洞 / Seitokai ni mo Ana wa Aru][05][1080p HEVC][CHS&CHT][简繁内封]"),
    ("【10月】学生会也有洞！ 01【限制解除版】.mp4", "【10月】学生会也有洞！ 01【TV放送版】.mp4"),
    ("[X] 某番 - 01 [規制解除版][1080p]", "[X] 某番 - 01 [1080p]"),
])
def test_uncut_words_score_as_uncensored(uncut, tv):
    rules = preferences.load_rules(preferences.PREFS_PATH)
    uncensored = next(r for r in rules["prefer"] if r["name"] == "无删减")
    assert preferences.score_only(uncut, rules) - preferences.score_only(tv, rules) == uncensored["weight"]
