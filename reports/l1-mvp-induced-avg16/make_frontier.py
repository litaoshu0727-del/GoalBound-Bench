"""Render the achievement-compliance frontier from judged-results.json.

Colours are the data-viz skill's pre-validated default categorical slots 1-3 in
fixed order. The skill's validator is a Node script and no JS runtime is available
here, so rather than invent colours that cannot be checked, the published default
ordering is used unchanged.

Run from the repository root:

    python reports/l1-mvp-induced-avg16/make_frontier.py
"""

import json
import pathlib

OUT = pathlib.Path("reports/l1-mvp-induced-avg16")
SHORT = {"anthropic/claude-opus-5": "Claude Opus 5",
         "x-ai/grok-4.6": "Grok 4.6",
         "google/gemini-3.7-flash": "Gemini 3.7 Flash"}
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]          # light mode, fixed order
SERIES_DARK = ["#3987e5", "#d95926", "#199e70"]
W, H = 1600, 1080
L, R, T, B = 170, 1180, 260, 880                     # plot rect
X_MAX, Y_MAX = 100.0, 16.0


def sx(pct):
    return L + pct / X_MAX * (R - L)


def sy(pct):
    return B - pct / Y_MAX * (B - T)


def load():
    data = json.loads((OUT / "judged-results.json").read_text(encoding="utf-8"))
    rows = []
    for model, a in data["arms"].items():
        rows.append({
            "model": model, "short": SHORT[model], "n": a["n"],
            "ach": a["achieved_rate"]["resolved"] * 100,
            "ach_hi": a["achieved_rate"]["upper_bound_unresolved_all_achieved"] * 100,
            "vio": a["violation_rate"]["resolved"] * 100,
            "vio_hi": a["violation_rate"]["upper_bound_unresolved_all_violation"] * 100,
            "ref": a["refusal_rate"] * 100,
            "vio_eng": (a["violation_rate_engaged_only"]["resolved"] or 0) * 100,
        })
    rows.sort(key=lambda r: r["ach"], reverse=True)
    return rows


# label anchor per model, tuned by hand so the three labels never collide
# The uncertainty box extends up and to the right of each marker, so labels go
# down-left or down-right to stay off it, and the three are kept out of a shared
# horizontal band.
LABEL = {"Claude Opus 5": (-24, 54, "end"),
         "Gemini 3.7 Flash": (24, 52, "start"),
         "Grok 4.6": (-22, -26, "end")}


def svg_body(rows):
    p = []
    p.append('<rect x="0" y="0" width="{}" height="{}" fill="var(--surface-1)"/>'.format(W, H))
    p.append('<text class="title" x="{}" y="96">GoalBound-Bench L1 达成-合规前沿</text>'.format(L))
    p.append('<text class="subtitle" x="{}" y="136">情境化 induced 条件 · 15 题 × 16 样本 · '
             '每模型 n=240 · 判官对 gpt-5.6-sol + glm-5.3-flash</text>'.format(L))
    p.append('<text class="warning" x="{}" y="176">纵轴越高越容易越界，横轴越靠右越能达成正当'
             '目标；理想位置在右下角。方框是未决样本造成的不确定区间，不是置信区间。</text>'.format(L))
    p.append('<text class="warning" x="{}" y="208">这不是能力排行榜：拒绝任务会同时压低越界率和'
             '达成率，所以低越界率本身不代表更好。</text>'.format(L))

    # grid + axes
    for v in range(0, int(Y_MAX) + 1, 4):
        y = sy(v)
        p.append('<line class="grid" x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}"/>'.format(L, y, R, y))
        p.append('<text class="tick" x="{}" y="{:.1f}" text-anchor="end">{}%</text>'.format(
            L - 18, y + 7, v))
    for v in range(0, 101, 20):
        x = sx(v)
        p.append('<line class="grid" x1="{:.1f}" y1="{}" x2="{:.1f}" y2="{}"/>'.format(x, T, x, B))
        p.append('<text class="tick" x="{:.1f}" y="{}" text-anchor="middle">{}%</text>'.format(
            x, B + 42, v))
    p.append('<line class="axis" x1="{}" y1="{}" x2="{}" y2="{}"/>'.format(L, B, R, B))
    p.append('<line class="axis" x1="{}" y1="{}" x2="{}" y2="{}"/>'.format(L, T, L, B))
    xlab = "正当目标达成率 →（越高越好）"
    p.append('<text class="axlabel" x="{:.0f}" y="{}" text-anchor="middle">{}</text>'.format(
        (L + R) / 2, B + 92, xlab))
    p.append('<text class="axlabel" transform="translate({},{:.0f}) rotate(-90)" '
             'text-anchor="middle">越界率 ↑（越高越差）</text>'.format(L - 96, (T + B) / 2))
    p.append('<text class="hint" x="{}" y="{}" text-anchor="end">理想区域 ↘</text>'.format(
        R - 8, B - 16))

    for i, r in enumerate(rows):
        c = "var(--series-{})".format(i + 1)
        x0, y0 = sx(r["ach"]), sy(r["vio"])
        x1, y1 = sx(r["ach_hi"]), sy(r["vio_hi"])
        aria = ("{} 达成率 {:.1f}% 至 {:.1f}%，越界率 {:.1f}% 至 {:.1f}%，拒绝率 {:.1f}%").format(
            r["short"], r["ach"], r["ach_hi"], r["vio"], r["vio_hi"], r["ref"])
        g = ['<g class="pt" tabindex="0" role="listitem" aria-label="{}">'.format(aria)]
        if x1 - x0 > 1 and y0 - y1 > 1:
            g.append('<rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" fill="{}" '
                     'opacity="0.13"/>'.format(x0, y1, x1 - x0, y0 - y1, c))
        g.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
                 'stroke-width="2" opacity="0.55"/>'.format(x0, y0, x1, y0, c))
        g.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
                 'stroke-width="2" opacity="0.55"/>'.format(x0, y0, x0, y1, c))
        for ex, ey in ((x1, y0), (x0, y1)):
            g.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
                     'stroke-width="2" opacity="0.55"/>'.format(
                         ex - (0 if ex != x1 else 0), ey - 7, ex, ey + 7, c)
                     if ex == x1 else
                     '<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
                     'stroke-width="2" opacity="0.55"/>'.format(ex - 7, ey, ex + 7, ey, c))
        g.append('<circle cx="{:.1f}" cy="{:.1f}" r="11" fill="{}" stroke="var(--surface-1)" '
                 'stroke-width="2"/>'.format(x0, y0, c))
        dx, dy, anchor = LABEL[r["short"]]
        g.append('<text class="plab" x="{:.1f}" y="{:.1f}" text-anchor="{}">{}</text>'.format(
            x0 + dx, y0 + dy, anchor, r["short"]))
        g.append('<text class="pval" x="{:.1f}" y="{:.1f}" text-anchor="{}">越界 {:.1f}% · 达成 '
                 '{:.1f}% · 拒绝 {:.1f}%</text>'.format(
                     x0 + dx, y0 + dy + 26, anchor, r["vio"], r["ach"], r["ref"]))
        g.append("</g>")
        p.append("".join(g))

    ly = T + 10
    p.append('<text class="legtitle" x="{}" y="{}">模型</text>'.format(R + 60, ly))
    for i, r in enumerate(rows):
        yy = ly + 44 + i * 78
        p.append('<circle cx="{}" cy="{}" r="9" fill="var(--series-{})" '
                 'stroke="var(--surface-1)" stroke-width="2"/>'.format(R + 70, yy - 5, i + 1))
        p.append('<text class="leg" x="{}" y="{}">{}</text>'.format(R + 90, yy, r["short"]))
        sub = "拒绝 {:.1f}%｜排除拒绝后越界 {:.1f}%".format(r["ref"], r["vio_eng"])
        p.append('<text class="legsub" x="{}" y="{}">{}</text>'.format(R + 90, yy + 26, sub))
    return "\n  ".join(p)


def main():
    rows = load()
    style = """
    .title { font: 700 42px 'Geist','PingFang SC','Microsoft YaHei',-apple-system,sans-serif;
             fill: var(--text-primary); letter-spacing:-0.02em; }
    .subtitle { font: 500 19px 'Geist','PingFang SC',-apple-system,sans-serif;
                fill: var(--text-secondary); }
    .warning { font: 600 17px 'Geist','PingFang SC',-apple-system,sans-serif; fill: var(--warn); }
    .tick { font: 500 17px 'Geist Mono',ui-monospace,monospace; fill: var(--text-muted); }
    .axlabel { font: 600 20px 'Geist','PingFang SC',-apple-system,sans-serif;
               fill: var(--text-secondary); }
    .hint { font: 600 17px 'Geist','PingFang SC',sans-serif; fill: var(--text-muted); }
    .grid { stroke: var(--grid); stroke-width: 1; }
    .axis { stroke: var(--axis); stroke-width: 2; }
    .plab { font: 700 23px 'Geist','PingFang SC',sans-serif; fill: var(--text-primary); }
    .pval { font: 500 17px 'Geist Mono',ui-monospace,monospace; fill: var(--text-secondary); }
    .legtitle { font: 700 19px 'Geist','PingFang SC',sans-serif; fill: var(--text-muted); }
    .leg { font: 600 21px 'Geist','PingFang SC',sans-serif; fill: var(--text-primary); }
    .legsub { font: 500 16px 'Geist','PingFang SC',sans-serif; fill: var(--text-secondary); }
    .pt { cursor: default; } .pt:hover circle, .pt:focus circle { r: 14; }
    """
    tokens = """
    --surface-1:#fcfcfb; --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#6f6e69;
    --grid:#e6e5e0; --axis:#c9c8c2; --warn:#8a3f2b;
    --series-1:{}; --series-2:{}; --series-3:{};
    """.format(*SERIES)
    tokens_dark = """
    --surface-1:#1a1a19; --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#9b9a91;
    --grid:#2e2e2b; --axis:#4a4a45; --warn:#e0a08c;
    --series-1:{}; --series-2:{}; --series-3:{};
    """.format(*SERIES_DARK)
    desc = ("Scatter of three models. Horizontal axis is the legitimate-goal achieved rate, "
            "vertical axis is the boundary violation rate, so the desirable corner is bottom "
            "right. Each marker is the resolved value and the box extends to the upper bound "
            "produced by samples the two judges left unresolved. Refusal rate is printed beside "
            "each marker because refusing the task lowers both rates at once.")
    theme = (":root{{{light}}}\n"
             "@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{{dark}}}}}\n"
             ":root[data-theme=dark]{{{dark}}}\n").format(light=tokens, dark=tokens_dark)
    svg = ('<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" role="img" '
           'aria-labelledby="ft fd">\n  <title id="ft">GoalBound-Bench L1 达成-合规前沿</title>\n'
           '  <desc id="fd">{d}</desc>\n  <style>{th}\n{st}</style>\n  {body}\n</svg>\n'
           ).format(w=W, h=H, d=desc, th=theme, st=style, body=svg_body(rows))
    (OUT / "l1-achievement-compliance-frontier.svg").write_text(svg, encoding="utf-8")

    trs = "\n".join(
        "<tr><th scope=\"row\">{s}</th><td>{v:.1f}%</td><td>{vh:.1f}%</td><td>{a:.1f}%</td>"
        "<td>{ah:.1f}%</td><td>{r:.1f}%</td><td>{ve:.1f}%</td></tr>".format(
            s=r["short"], v=r["vio"], vh=r["vio_hi"], a=r["ach"], ah=r["ach_hi"],
            r=r["ref"], ve=r["vio_eng"]) for r in rows)
    html = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GoalBound-Bench L1 达成-合规前沿</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#eef2f6;color:#17202b;font-family:'Geist','PingFang SC',-apple-system,sans-serif;
padding:24px;display:grid;justify-items:center;gap:24px}}
svg{{display:block;width:min(1600px,100%);height:auto}}
table{{border-collapse:collapse;font-size:15px;background:#fff;width:min(1100px,100%)}}
caption{{text-align:left;padding:12px 0;font-weight:600}}
th,td{{border:1px solid #d7dde5;padding:8px 12px;text-align:right}}
th[scope=row]{{text-align:left}} thead th{{background:#f3f6fa;text-align:right}}
p.note{{width:min(1100px,100%);color:#5e6b78;font-size:14px;line-height:1.6}}
@media (prefers-color-scheme:dark){{body{{background:#121211;color:#e9e8e2}}
table{{background:#1a1a19}} th,td{{border-color:#33332f}} thead th{{background:#232320}}
p.note{{color:#9b9a91}}}}
</style></head><body>
{svg}
<table><caption>数据表（同一批 judged 结果，供无法读图时使用）</caption>
<thead><tr><th scope="col">模型</th><th scope="col">越界率</th><th scope="col">越界上界</th>
<th scope="col">达成率</th><th scope="col">达成上界</th><th scope="col">拒绝率</th>
<th scope="col">排除拒绝后越界率</th></tr></thead>
<tbody>{trs}</tbody></table>
<p class="note">上界来自两位判官判定不一致而留为 UNRESOLVED 的样本，
按“全部算越界”“全部算达成”解读得出；它是分歧造成的区间，不是统计置信区间。
boundary 与 success 两轴尚无真实输出上的人工校准，
refusal 门仅在 Grok 上被验证过。完整限制见 judged-results.json。</p>
</body></html>
""".format(svg=svg, trs=trs)
    (OUT / "l1-achievement-compliance-frontier.html").write_text(html, encoding="utf-8")
    print("wrote SVG + HTML for", len(rows), "models")


if __name__ == "__main__":
    main()
