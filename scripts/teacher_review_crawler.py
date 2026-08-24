# -*- coding: utf-8 -*-
"""
林职院老师评价·按需爬取脚本（校园百事通 Skill 附属工具）
================================================================
定位：这是一个「特定功能」脚本 —— Skill 数据文件中**不内置任何老师评价数据**
     （保护老师隐私、且同学评价主观性强），只有当 Skill 使用者明确提出
     "想了解某位老师的评价"时，才由 Skill 调用本脚本现场爬取。

用途：从公开网络（Bing / DuckDuckGo 检索贴吧、小红书、知乎等公开帖）
     收集与该老师相关的公开评价性提及，生成 Markdown 报告供参考。

使用方法：
    python scripts/teacher_review_crawler.py "张三"
    python scripts/teacher_review_crawler.py "张三" --dept 信息工程系
    python scripts/teacher_review_crawler.py "张三" --course 高等数学
    python scripts/teacher_review_crawler.py "张三" --out D:/reviews

输出：teacher_review_<姓名>_<时间戳>.md（默认在系统临时目录，
     报告仅作为一次性参考，**不会写入 references/ 数据文件**）

红线：
    - 只采集公开网络内容，不做任何需要登录/破解的操作
    - 网络评价 ≠ 官方事实，报告首尾均带"仅供参考"免责声明
    - 手机号/身份证/QQ 号等隐私信息自动打码
"""
import argparse
import html as html_mod
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime

SCHOOL = "福建林业职业技术学院"
SCHOOL_SHORT = "林职院"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

# 隐私打码：手机号 / 身份证 / QQ 号
PRIVACY_PATTERNS = [
    (re.compile(r"1[3-9]\d{9}"), "[手机号已打码]"),
    (re.compile(r"\d{17}[\dXx]"), "[身份证已打码]"),
    (re.compile(r"[Qq][Qq]\s*[:：]?\s*\d{6,12}"), "QQ:[已打码]"),
]


def mask_privacy(text):
    """隐私信息打码"""
    for pat, rep in PRIVACY_PATTERNS:
        text = pat.sub(rep, text)
    return text


def strip_tags(fragment):
    """去除 HTML 标签并解码实体"""
    text = re.sub(r"<[^>]+>", "", fragment or "")
    return html_mod.unescape(text).strip()


def http_get(url, timeout=15, ua=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": ua or UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


# ---------------------------------------------------------------- 搜索引擎
def bing_search(query, count=8):
    """Bing 网页搜索，返回 [{title,url,snippet}]"""
    results = []
    try:
        page = http_get("https://www.bing.com/search?q=" + urllib.parse.quote(query)
                        + "&count=%d&setlang=zh-CN" % (count * 2))
        for block in re.findall(r'<li class="b_algo".*?</li>', page, re.S):
            m_url = re.search(r'<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if not m_url:
                continue
            url = m_url.group(1)
            title = strip_tags(m_url.group(2))
            m_snip = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
            snippet = strip_tags(m_snip.group(1)) if m_snip else ""
            if title and url.startswith("http"):
                results.append({"title": title, "url": url, "snippet": snippet,
                                "engine": "Bing", "query": query})
            if len(results) >= count:
                break
    except Exception as e:
        print("[warn] Bing 搜索失败: %s" % e, file=sys.stderr)
    return results


def duckduckgo_search(query, count=8):
    """DuckDuckGo HTML 版搜索（对脚本请求友好，但限流时返回 anomaly 页）"""
    results = []
    try:
        page = http_get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query))
        if "anomaly" in page.lower():
            print("[warn] DuckDuckGo 触发限流验证，跳过该引擎", file=sys.stderr)
            return results
        for m in re.finditer(
                r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
            url = m.group(1)
            # DDG 跳转链接解包
            if "uddg=" in url:
                url = urllib.parse.unquote(url.split("uddg=")[-1].split("&")[0])
            title = strip_tags(m.group(2))
            if title and url.startswith("http"):
                results.append({"title": title, "url": url, "snippet": "",
                                "engine": "DuckDuckGo", "query": query})
            if len(results) >= count:
                break
        # 补 snippet
        snips = re.findall(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
        for i, s in enumerate(snips):
            if i < len(results) and not results[i]["snippet"]:
                results[i]["snippet"] = strip_tags(s)
    except Exception as e:
        print("[warn] DuckDuckGo 搜索失败: %s" % e, file=sys.stderr)
    return results


def baidu_search(query, count=8):
    """百度网页搜索（中文结果质量高，链接为百度跳转链）"""
    results = []
    try:
        page = http_get("https://www.baidu.com/s?wd=" + urllib.parse.quote(query))
        # 以 <h3> 为界切块，取标题链接 + 后续摘要
        segments = re.split(r"<h3[^>]*>", page)[1:]
        for seg in segments:
            m = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', seg, re.S)
            if not m:
                continue
            url = m.group(1)
            title = strip_tags(m.group(2))
            m_snip = re.search(r'<div[^>]*class="[^"]*(?:c-abstract|content-right)[^"]*"[^>]*>(.*?)</div>', seg, re.S)
            snippet = strip_tags(m_snip.group(1)) if m_snip else ""
            if title and url.startswith("http"):
                results.append({"title": title, "url": url, "snippet": snippet,
                                "engine": "Baidu", "query": query})
            if len(results) >= count:
                break
    except Exception as e:
        print("[warn] 百度搜索失败: %s" % e, file=sys.stderr)
    return results


def sogou_m_search(query, count=8):
    """移动版搜狗搜索（m.sogou.com）—— 实测反爬最宽容的中文通道。

    经验教训：桌面版搜狗（www.sogou.com/web）被反爬标记后会持续弹
    antispider 验证页；而移动版（移动 UA + searchList.jsp）对脚本请求
    长期保持可用，且中文结果质量与桌面版一致 → 作为链首引擎。
    结果页含"大家还在搜"等推荐块，靠标题非空 + URL 合法过滤。
    """
    results = []
    try:
        page = http_get("https://m.sogou.com/web/searchList.jsp?keyword="
                        + urllib.parse.quote(query), ua=UA_MOBILE)
        if "antispider" in page.lower() or "请输入验证码" in page:
            print("[warn] 移动版搜狗触发反爬验证，跳过该引擎", file=sys.stderr)
            return results
        # 移动版结果块：<h3><a href=...>标题</a></h3>
        for m in re.finditer(r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                             page, re.S):
            url = m.group(1)
            if url.startswith("/link") or url.startswith("/web"):
                url = "https://m.sogou.com" + url
            title = strip_tags(m.group(2))
            if not title or not url.startswith("http"):
                continue
            if title in ("大家还在搜", "相关搜索"):
                continue
            results.append({"title": title, "url": url, "snippet": "",
                            "engine": "SogouM", "query": query})
            if len(results) >= count:
                break
    except Exception as e:
        print("[warn] 移动版搜狗搜索失败: %s" % e, file=sys.stderr)
    return results


def shenma_search(query, count=8):
    """神马搜索（quark.sm.cn，阿里系移动引擎）—— 实测对脚本请求宽容。

    经验教训：当百度/搜狗/DDG 都被反爬标记后，神马往往仍可用；
    结果以移动端网页为主（含百家号/头条号等），与小红书/抖音
    同为移动生态，正好覆盖同学讨论场景。
    """
    results = []
    try:
        page = http_get("https://quark.sm.cn/s?q=" + urllib.parse.quote(query),
                        ua=UA_MOBILE)
        for m in re.finditer(
                r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.{5,120}?)</a>', page, re.S):
            url, title = m.group(1), strip_tags(m.group(2))
            if not title or len(title) < 4:
                continue
            # 过滤导航类链接
            if any(url.startswith(d) for d in
                   ("https://quark.sm.cn", "https://m.sm.cn")):
                continue
            results.append({"title": title, "url": url, "snippet": "",
                            "engine": "Shenma", "query": query})
            if len(results) >= count:
                break
    except Exception as e:
        print("[warn] 神马搜索失败: %s" % e, file=sys.stderr)
    return results


def sogou_search(query, count=8):
    """搜狗网页搜索（中文质量高、对脚本请求宽容，作为中坚引擎）"""
    results = []
    try:
        page = http_get("https://www.sogou.com/web?query=" + urllib.parse.quote(query))
        if "antispider" in page.lower() or "请输入验证码" in page:
            print("[warn] 搜狗触发反爬验证，跳过该引擎", file=sys.stderr)
            return results
        segments = re.split(r"<h3[^>]*>", page)[1:]
        for seg in segments:
            m = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', seg, re.S)
            if not m:
                continue
            url = m.group(1)
            if url.startswith("/link"):
                url = "https://www.sogou.com" + url  # 搜狗跳转链
            title = strip_tags(m.group(2))
            m_snip = re.search(r'<(?:div|p)[^>]*class="[^"]*(?:txt|abstract|space)[^"]*"[^>]*>(.*?)</(?:div|p)>', seg, re.S)
            snippet = strip_tags(m_snip.group(1)) if m_snip else ""
            if title and url.startswith("http"):
                results.append({"title": title, "url": url, "snippet": snippet,
                                "engine": "Sogou", "query": query})
            if len(results) >= count:
                break
    except Exception as e:
        print("[warn] 搜狗搜索失败: %s" % e, file=sys.stderr)
    return results


def _engine_sane(query, results):
    """引擎降级检测：若全部结果的标题+摘要都不含查询词的任一完整分词，
    说明引擎返回了泛化降级结果（如 Bing 无 Cookie 时查老师返回"福建省"百科），整组丢弃。"""
    tokens = [t for t in query.split() if len(t) >= 2]
    if not tokens:
        return True
    for r in results:
        text = r["title"] + r["snippet"]
        if any(tok in text for tok in tokens):
            return True
    return False


def search(query, count=8, name=None):
    """多引擎链式检索（移动搜狗 → 神马 → 搜狗 → DDG → 百度 → Bing），
    带引擎降级检测与姓名过滤。

    经验教训：
    1. Bing 对无 Cookie 的脚本请求经常返回"降级泛结果"（查某老师却返回
       "福建省"百科），数量够但全不相关 → 用分词覆盖检测整组丢弃；
    2. DDG 限流快（anomaly 页）；百度限流后弹安全验证；桌面版搜狗被标记后
       持续 antispider；**移动版搜狗与神马（quark.sm.cn）对脚本最宽容** → 链首；
    3. 限流是常态，多引擎互为备份是必需的。
    """
    def relevant(r):
        if not name:
            return True
        return name in (r["title"] + r["snippet"])

    merged, seen = [], set()

    def run_chain():
        for engine in (sogou_m_search, shenma_search, sogou_search, duckduckgo_search, baidu_search, bing_search):
            if len(merged) >= count:
                break
            try:
                raw = engine(query, count)
            except Exception as e:
                print("[warn] %s 异常: %s" % (engine.__name__, e), file=sys.stderr)
                continue
            if raw and not _engine_sane(query, raw):
                print("[warn] %s 返回降级泛结果，已丢弃" % engine.__name__, file=sys.stderr)
                continue
            for r in raw:
                if not relevant(r):
                    continue
                key = r["url"].split("#")[0]
                if key in seen:
                    continue
                seen.add(key)
                merged.append(r)
                if len(merged) >= count:
                    break

    run_chain()
    # 全引擎限流时的退避重试（限流通常几十秒~几分钟自动恢复）
    if not merged:
        print("[i] 所有引擎均被限流，等待 30 秒后重试一次…", file=sys.stderr)
        time.sleep(30)
        run_chain()

    return merged


# ---------------------------------------------------------------- 站点识别
def classify_site(url):
    """给结果贴来源标签"""
    u = url.lower()
    if "tieba.baidu.com" in u:
        return "百度贴吧"
    if "xiaohongshu.com" in u:
        return "小红书"
    if "zhihu.com" in u:
        return "知乎"
    if "douyin.com" in u:
        return "抖音"
    if "bilibili.com" in u:
        return "B站"
    if "weibo.com" in u:
        return "微博"
    if "fjlzy.com" in u:
        return "学校官网"
    return "其他网站"


# ---------------------------------------------------------------- 主流程
def build_queries(name, dept, course):
    """构造多角度检索词"""
    q = [f"{SCHOOL} {name} 老师",
         f"{name} {SCHOOL_SHORT} 上课 怎么样",
         f"site:tieba.baidu.com 林职院 {name}",
         f"site:xiaohongshu.com 林职院 {name}"]
    if dept:
        q.append(f"{SCHOOL} {dept} {name}")
    if course:
        q.append(f"{name} {course} 老师 评价")
    return q


# ------------------------------------------------------- 候选发现模式（--discover）
NAME_STOPWORDS = {
    "同学", "学生", "大家", "宝贝", "辅导", "体育", "音乐", "美术", "英语",
    "数学", "高数", "上课", "那位", "这位", "哪个", "每个", "有个", "一位",
    "美女", "帅哥", "我们", "你们", "他们", "什么", "怎么", "谢谢", "哈哈",
    "美女老", "导员", "学姐", "学长", "老师", "教的", "任课", "班主任",
}


def extract_teacher_names(texts, urls=None):
    """从标题/摘要中启发式抽取老师姓名：
    ① 「XX老师」称呼；② 百度百科词条式「XX(福建林业职业技术学院...」；
    ③ 「XX 副教授/教授/讲师」职称式；④ 官网页标题式「XX-福建林业职业技术学院」"""
    names = {}
    joined = "\n".join(texts)
    patterns = [
        r"([\u4e00-\u9fa5]{2,4})老师",
        r"([\u4e00-\u9fa5]{2,4})[（(]福建林业职业技术学院",
        r"([\u4e00-\u9fa5]{2,4})\s*(?:副教授|教授|讲师|高级工程师)",
    ]
    for pat in patterns:
        for m in re.finditer(pat, joined):
            n = m.group(1)
            if n in NAME_STOPWORDS or len(n) < 2:
                continue
            # 过滤明显是修饰词的（如"最美老师""好老师"）
            if n in ("最美", "好的", "厉害", "宝藏", "喜欢", "讨厌"):
                continue
            # 过滤动词短语噪音（如"值得当老师"抽出"值得当"）
            if n[-1] in "当是的了要想能会在被把地和很":
                continue
            names[n] = names.get(n, 0) + 1
    # ④ 官网页标题式：url 为 fjlzy.com 且标题以「中文姓名-福建林业…」开头
    if urls:
        for url, title in zip(urls, texts):
            if "fjlzy.com" in url:
                m = re.match(r"^([\u4e00-\u9fa5]{2,4})\s*[-·—]\s*福建林业", title.strip())
                if m and m.group(1) not in NAME_STOPWORDS:
                    names[m.group(1)] = names.get(m.group(1), 0) + 2  # 官方源加权
    return names


def discover(topic, out_dir):
    """发现模式：泛话题检索 → 列出被提及的老师候选清单，供用户选择后深挖"""
    topic = (topic or "宝藏老师").strip()
    queries = [
        f"{SCHOOL} {topic}",
        f"{SCHOOL_SHORT} 最喜欢的老师",
        f"{SCHOOL_SHORT} 老师 推荐 评价",
        f"site:xiaohongshu.com {SCHOOL_SHORT} 老师",
        f"site:tieba.baidu.com {SCHOOL_SHORT} 老师 评价",
    ]
    if topic != "宝藏老师":
        queries.insert(0, f"{SCHOOL_SHORT} {topic}")

    results, seen = [], set()
    for query in queries:
        for r in search(query, count=8):
            key = r["url"].split("#")[0]
            if key in seen:
                continue
            seen.add(key)
            # 学校相关性过滤：排除其他林职院（云南/河南/山西/黑龙江…）
            text = r["title"] + r["snippet"] + r["url"]
            if not any(k in text for k in ("福建", "fjlzy", "林职院")):
                continue
            r["site"] = classify_site(r["url"])
            results.append(r)
        time.sleep(1.5)

    names = extract_teacher_names(
        [r["title"] + " " + r["snippet"] for r in results],
        urls=[r["url"] for r in results])
    # 按提及次数排序
    ranked = sorted(names.items(), key=lambda kv: -kv[1])

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"teacher_candidates_{ts}.md")
    lines = [
        f"# 林职院「{topic}」候选老师发现清单",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 检索条目：{len(results)} 条公开网页",
        f"- 识别出被提及的老师：{len(ranked)} 位",
        "",
        "> ⚠️ 名单来自公开网络提及（同学/网友发言 + 官方报道），不代表官方评价。",
        "",
    ]
    if ranked:
        lines += ["| 候选老师 | 网络提及次数 |", "|---------|------------|"]
        for n, c in ranked[:20]:
            lines.append(f"| {n} | {c} |")
        lines += [
            "",
            "**下一步**：告诉 Skill 你想深入了解哪位老师（如「罗春玉老师上课怎么样」），",
            "它会运行本脚本（不带 --discover）生成该老师的评价参考报告。",
            "",
        ]
    else:
        lines += [
            "本轮检索未从标题/摘要中识别出具体老师姓名。建议：",
            "1. 换个话题词重试（如「最喜欢的老师」「宝藏教师」）；",
            "2. 直接去小红书/抖音 App 搜「林职院+老师」翻评论区。",
            "",
        ]
    # 无论是否识别出姓名，都列出相关帖子（评论区线索）
    if results:
        lines += [
            "## 相关帖子（评论区线索）",
            "",
            "> 💡 提示：同学评价大量藏在小红书/抖音帖子的**评论区**，",
            "> 上述平台网页版有登录墙，建议同时打开 App 搜索关键词直接看评论区。",
            "",
        ]
        for r in results[:12]:
            lines.append(f"- [{r['title']}]({r['url']})（{r['site']}）")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path, ranked, results


# ------------------------------------------------------- 帖子正文/评论区提及提取
LOGIN_WALL_HINTS = ("请登录", "扫码登录", "登录后", "验证码", "访问异常")
# 评论区可直抓的站点（无强登录墙）
COMMENT_FRIENDLY = ("tieba.baidu.com", "fjlzy.com", "yurenhao.sizhengwang.cn",
                    "zhihu.com", "bilibili.com", "baike.baidu.com")


def extract_mentions(url, name, max_sentences=6):
    """抓取帖子页面，提取正文中含老师姓名的句子（贴吧等开放站点可覆盖到楼中楼评论）。

    小红书/抖音网页版有登录墙：检测到墙就如实返回提示，不硬闯。
    """
    try:
        page = http_get(url, timeout=12)
    except Exception:
        return None, "页面抓取失败（可能已失效或有反爬）"

    # 登录墙检测
    if any(h in page for h in LOGIN_WALL_HINTS) and name not in page:
        return None, "该平台有登录墙，网页版看不到正文/评论——建议打开 App 搜同标题看评论区"

    # 去掉 script/style 再抽正文
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    text = html_mod.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"\s+", " ", text)

    sentences = re.split(r"[。！？!?\n]+", text)
    hits = [s.strip() for s in sentences if name in s and 8 < len(s.strip()) < 300]
    hits = hits[:max_sentences]
    if not hits:
        return None, None
    return hits, None


def crawl(name, dept, course):
    all_results = []
    seen_urls = set()
    for query in build_queries(name, dept, course):
        # 姓名相关性过滤已在 search() 内完成（决定是否启用备用引擎）
        for r in search(query, name=name):
            key = r["url"].split("#")[0]
            if key in seen_urls:
                continue
            seen_urls.add(key)
            r["site"] = classify_site(r["url"])
            r["title"] = mask_privacy(r["title"])
            r["snippet"] = mask_privacy(r["snippet"])
            all_results.append(r)
        time.sleep(1.5)  # 礼貌间隔，避免请求过频

    # 追加抓取前几个帖子的正文/评论区提及（同学评价常藏在评论区）
    print("[i] 正在抓取相关帖子的正文与评论区提及…")
    for r in all_results[:6]:
        r["mentions"] = []
        r["mention_note"] = ""
        # 小红书/抖音网页版登录墙较硬，直接标注不硬闯
        if any(d in r["url"] for d in ("xiaohongshu.com", "douyin.com")):
            r["mention_note"] = "小红书/抖音网页版有登录墙——建议在 App 内搜该帖标题，翻评论区看同学讨论"
            continue
        mentions, note = extract_mentions(r["url"], name)
        if mentions:
            r["mentions"] = mentions
        elif note:
            r["mention_note"] = note
        time.sleep(1.0)
    return all_results


def write_report(name, dept, course, results, out_dir):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"teacher_review_{name}_{ts}.md")

    lines = [
        f"# 「{name}」老师网络评价参考报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 检索范围：公开网络（贴吧 / 小红书 / 知乎 / 抖音等公开帖及网页）",
        (f"- 系部：{dept}" if dept else "- 系部：未指定"),
        (f"- 课程：{course}" if course else "- 课程：未指定"),
        f"- 命中条目：{len(results)} 条",
        "",
        "> ⚠️ **免责声明**：以下内容全部来自同学和网友的公开网络发言，",
        "> 主观性强、时效性不明，**仅供参考，不代表官方评价**。",
        "> 老师授课安排等权威信息请以教务系统课表和学校官方通知为准。",
        "",
    ]

    if not results:
        lines += [
            "## 未检索到相关评价",
            "",
            "公开网络暂无与该老师相关的评价性内容。可能原因：",
            "1. 老师姓名较为常见或拼写不确定；",
            "2. 相关帖子未被搜索引擎收录；",
            "3. 确实没有公开讨论。",
            "",
            "建议：补全系部/课程名重试，或直接咨询学长学姐、班级群。",
        ]
    else:
        by_site = {}
        for r in results:
            by_site.setdefault(r["site"], []).append(r)
        for site, items in sorted(by_site.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"## {site}（{len(items)} 条）")
            lines.append("")
            for i, r in enumerate(items, 1):
                lines.append(f"### {i}. {r['title']}")
                lines.append(f"- 链接：{r['url']}")
                if r['snippet']:
                    lines.append(f"- 摘要：{r['snippet']}")
                for m in r.get("mentions", []):
                    lines.append(f"- 📎 正文/评论提及：{m}")
                if r.get("mention_note"):
                    lines.append(f"- ⚠️ {r['mention_note']}")
                lines.append("")
        lines += [
            "## 汇总提醒",
            "",
            "1. 以上均为**同学/网友个人观点**，同一老师不同学期、不同班级",
            "   的授课体验可能差异很大；",
            "2. 建议结合多方信息综合判断，避免因单条评价形成偏见；",
            "3. 如需权威信息（授课课程、职称等），请查询学校官网师资页或教务系统。",
            "",
            "> 🔒 本报告为一次性参考文件，不会被写入 Skill 数据文件，",
            "> 以保护老师隐私、避免主观评价被固化传播。",
        ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def main():
    # Windows 控制台默认 GBK，强制 UTF-8 输出避免乱码/报错
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="林职院老师评价·按需爬取（仅供参考）",
        epilog="两种用法：① --discover \"话题词\" 先列出候选老师；② 给出姓名生成该老师的评价报告")
    parser.add_argument("name", nargs="?", default="", help="老师姓名（与 --discover 二选一）")
    parser.add_argument("--discover", dest="topic", default="",
                        help="发现模式：按话题词（如\"宝藏老师\"）检索并列出候选老师清单")
    parser.add_argument("--dept", default="", help="系部（可选，提高准确度）")
    parser.add_argument("--course", default="", help="课程名（可选，提高准确度）")
    parser.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "fjlzy_reviews"),
                        help="报告输出目录（默认系统临时目录）")
    args = parser.parse_args()

    # 模式一：候选发现（用户泛问"有哪些宝藏老师"时）
    if args.topic or not args.name:
        topic = args.topic or "宝藏老师"
        print(f"[i] 发现模式：检索「{topic}」相关公开提及…（{datetime.now():%H:%M:%S}）")
        path, ranked, results = discover(topic, args.out)
        print(f"[✓] 候选清单已生成：{path}")
        if ranked:
            top = "、".join(f"{n}（{c}次）" for n, c in ranked[:5])
            print(f"[i] 被提及最多的老师：{top}")
            print("[!] 请用户从清单中选择想深入了解的老师后，再用姓名模式运行本脚本")
        else:
            print("[i] 未识别出具体老师姓名，建议换话题词或直接翻小红书/抖音评论区")
        return

    # 模式二：指定老师深挖
    print(f"[i] 开始检索「{args.name}」的公开网络评价…（{datetime.now():%H:%M:%S}）")
    results = crawl(args.name.strip(), args.dept.strip(), args.course.strip())
    print(f"[i] 检索完成，命中 {len(results)} 条相关内容")

    path = write_report(args.name.strip(), args.dept.strip(), args.course.strip(),
                        results, args.out)
    print(f"[✓] 报告已生成：{path}")
    print("[!] 提醒：内容均来自网络公开发言，仅供参考，不代表官方评价")


if __name__ == "__main__":
    main()
