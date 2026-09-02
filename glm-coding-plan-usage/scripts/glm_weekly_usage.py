#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GLM Coding Plan 按 key 周用量报告生成器。

通过本机 Chrome 中已登录的智谱开放平台会话（cookie bigmodel_token_production），
调用控制台内部接口拉取「费用明细」（按 API Key 出账数据），聚合出上周（周一~周日）
每个 key 的 token 用量与调用次数，输出微信友好的 Markdown 报告。

聚合口径（与接口核实）：
- token 量 = 各行 usageCount 按 tokenType 显式分类：输入 / 输出 / 缓存命中 三类分别累加；
  未知新类型单列 otherTok，报告中显式标注，不静默归入输入。
- apiUsage（调用次数）在同一 (key, day, model) 的输入/输出行重复出现，
  故按 (key, day, model) 取 max 后再对 (key, day) 求和。
- 费用明细接口分页返回 {total, rows[]}：逐页拉取并校验累计行数==total，
  取不齐时报错退出，绝不输出缩水报告。
- 聚合一律以 apiKey 全量 ID 为键，名称仅用于展示（同名 key 附 ID 尾 4 位区分）。
- 账单按天出账，当天数据不出现；周日无出账记录时报告口径行如实说明。

退出码：0=成功；1=参数错误；2=需要重新登录（NEED_LOGIN）；3=Chrome/页面/权限不可用；4=接口错误。
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import date, timedelta

POLL_MAX = 40  # seconds
BILL_PAGE_SIZE = 500
BILL_MAX_PAGES = 100  # safety bound, 100*500=50k rows/月远超实际


def run_osa(script, timeout=90):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "").strip(), r.returncode, (r.stderr or "").strip()


def classify_osa_failure(err):
    """osascript 失败分类：权限/环境问题返回 CHROME_PERM（退出码 3 语义），其余 OSA_ERR。"""
    low = (err or "").lower()
    if "1743" in (err or ""):
        return "CHROME_PERM: 未授权向 Chrome 发送 Apple Events（系统设置→隐私与安全性→自动化）"
    if "javascript" in low and ("allow" in low or "turned off" in low
                                or "not authorized" in low or "允许" in (err or "")):
        return "CHROME_PERM: 需在 Chrome 开启「视图→开发者→允许 Apple Events 中的 JavaScript」"
    return "OSA_ERR:" + (err or "")[:150]


def wrap_js(js_oneline, tab_ref):
    wi, ti = tab_ref
    esc = js_oneline.replace("\\", "\\\\").replace('"', '\\"')
    return (f'tell application "Google Chrome" to tell tab {ti} of window id {wi} '
            f'to execute javascript "{esc}"')


def ensure_bigmodel_tab():
    """Return (ok, tab_ref|errmsg). 找到/新建 bigmodel 标签页并激活、窗口置前。

    成功后返回 (window_id, tab_index) 元组——window_id 是 Chrome 内部稳定 id，
    用户切换窗口/标签不影响引用；后续 fire/poll 都钉在这个具体 tab 上，
    每次 poll 前还会校验该 tab URL 仍是 open.bigmodel.cn。
    """
    script = '''tell application "Google Chrome"
    launch
    set ti to 0
    set wid to 0
    set found to false
    repeat with w in windows
        set ti to 0
        repeat with t in tabs of w
            set ti to ti + 1
            if URL of t contains "open.bigmodel.cn" then
                set active tab index of w to ti
                set wid to id of w
                set index of w to 1
                set found to true
                exit repeat
            end if
        end repeat
        if found then exit repeat
    end repeat
    if not found then
        if (count of windows) is 0 then
            make new window
        end if
        tell front window
            make new tab with properties {URL:"https://open.bigmodel.cn/coding-plan/personal/usage"}
            set ti to count of tabs
            set active tab index to ti
            set wid to id of front window
        end tell
    end if
    return (wid as text) & "," & ti
end tell'''
    out, rc, err = run_osa(script)
    if rc != 0:
        return False, classify_osa_failure(err)
    parts = out.split(",")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return False, "TAB_REF_BAD:" + out[:80]
    return True, (parts[0], parts[1])


def tab_url(tab_ref):
    wi, ti = tab_ref
    out, rc, err = run_osa(
        f'tell application "Google Chrome" to get URL of tab {ti} of window id {wi}')
    return out, rc


def fire_and_poll(js_fire, varname, tab_ref, poll_max=POLL_MAX):
    out, rc, err = run_osa(wrap_js(js_fire, tab_ref))
    if rc != 0:
        return None, classify_osa_failure(err)
    deadline = time.time() + poll_max
    while time.time() < deadline:
        time.sleep(2)
        url, urc = tab_url(tab_ref)
        if urc != 0 or "open.bigmodel.cn" not in url:
            return None, "TAB_LOST: 目标标签页被关闭或导航离开 open.bigmodel.cn"
        out, rc, err = run_osa(wrap_js(varname + ' || "pending"', tab_ref))
        if rc == 0 and out and out != "pending":
            if out.startswith('"') and out.endswith('"'):
                try:
                    out = json.loads(out)
                except Exception:
                    pass
            return out, None
    return None, "POLL_TIMEOUT"


def fmt_m(n):
    if n >= 1e6:
        return f"{n/1e6:.1f}M"
    if n >= 1000:
        return f"{n/1e3:.1f}k"
    return str(n)


def build_report(monday, sunday, payload):
    name_by_id = payload["nameById"]
    rows = payload["rows"]
    billed = set(payload.get("billedDays", []))
    # 同名 key 展示消歧：名称出现多次时附 ID 尾 4 位
    name_count = {}
    for kid, nm in name_by_id.items():
        name_count[nm] = name_count.get(nm, 0) + 1

    def label(kid):
        nm = name_by_id.get(kid)
        if nm is None:
            return (kid or "?")[:12]
        if name_count.get(nm, 0) > 1:
            return f"{nm}(…{kid[-4:]})"
        return nm

    # 聚合键一律为 apiKey 全量 ID，名称仅用于输出
    agg = {}
    day_tot = {}
    other_total = 0
    for o in rows:
        k = o["key"]
        a = agg.setdefault(k, [0, 0, 0, 0])
        a[0] += o["inTok"]
        a[1] += o["outTok"]
        a[2] += o["calls"]
        a[3] += o.get("cacheTok", 0)
        other_total += o.get("otherTok", 0)
        d = day_tot.setdefault(o["day"], [0, 0, 0])
        d[0] += o["inTok"] + o["outTok"]
        d[1] += o["calls"]
        d[2] += o.get("cacheTok", 0)
    total_tok = sum(v[0] + v[1] for v in agg.values())
    total_calls = sum(v[2] for v in agg.values())
    total_cache = sum(v[3] for v in agg.values())
    lines = []
    lines.append(f"GLM Coding Plan 周用量报告（{monday.isoformat()} ~ {sunday.isoformat()}）")
    lines.append("")
    lines.append("| Key | 输入 | 输出 | 缓存命中 | 调用次数 | 占比 |")
    lines.append("|---|---|---|---|---|---|")
    for kid, (i, o, c, ch) in sorted(agg.items(), key=lambda x: -(x[1][0] + x[1][1])):
        share = (i + o) / total_tok * 100 if total_tok else 0
        lines.append(f"| {label(kid)} | {fmt_m(i)} | {fmt_m(o)} | {fmt_m(ch)} | {c} | {share:.1f}% |")
    lines.append(f"| 合计 | {fmt_m(sum(v[0] for v in agg.values()))} | {fmt_m(sum(v[1] for v in agg.values()))} "
                 f"| {fmt_m(total_cache)} | {total_calls} | 100% |")
    zero = [label(k) for k in name_by_id if k not in agg]
    if zero:
        lines.append("")
        lines.append("本周零消耗：" + "、".join(zero))
    if day_tot:
        lines.append("")
        lines.append("账号每日（入+出 / 调用 / 缓存）：")
        wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        for d in sorted(day_tot):
            dt = date.fromisoformat(d)
            t, c, ch = day_tot[d]
            lines.append(f"- {wd[dt.weekday()]} {d}：{fmt_m(t)} / {c} 次 / 缓存 {fmt_m(ch)}")
    if other_total > 0:
        lines.append("")
        lines.append(f"注：接口出现未分类 token 类型共 {fmt_m(other_total)}，已单列、未计入上表。")
    lines.append("")
    if sunday.isoformat() in billed:
        lines.append(f"口径：智谱控制台费用明细（按 key 出账），占比按输入+输出计，"
                     f"数据截至 {sunday.isoformat()}，均已出账。")
    else:
        lines.append(f"口径：智谱控制台费用明细（按 key 出账），占比按输入+输出计，"
                     f"数据截至 {sunday.isoformat()}；"
                     "周日无出账记录（当天零用量或出账延迟）。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--monday", help="上周周一 YYYY-MM-DD（默认自动算上一个完整周）")
    args = ap.parse_args()
    today = date.today()
    if args.monday:
        try:
            monday = date.fromisoformat(args.monday)
        except ValueError:
            print("BAD_MONDAY: 格式应为 YYYY-MM-DD")
            sys.exit(1)
        if monday.weekday() != 0:
            print("BAD_MONDAY: 该日期不是周一")
            sys.exit(1)
    else:
        monday = today - timedelta(days=today.weekday() + 7)
    sunday = monday + timedelta(days=6)
    months = sorted({monday.strftime("%Y-%m"), sunday.strftime("%Y-%m")})

    ok, ref = ensure_bigmodel_tab()
    if not ok:
        print(ref)
        sys.exit(3)
    tab_ref = ref
    time.sleep(4)  # let page settle

    # login check（钉在同一个 tab 上）
    out, rc = tab_url(tab_ref)
    if rc == 0 and "/login" in out:
        print("NEED_LOGIN")
        sys.exit(2)

    js = (
        'window.__glm_report=null;'
        '(async function(){try{'
        'var tok=(document.cookie.match(/bigmodel_token_production=([^;]+)/)||[])[1];'
        'if(!tok){window.__glm_report=JSON.stringify({error:"NEED_LOGIN"});return;}'
        'var org=localStorage.getItem("Bigmodel-Organization")||"";'
        'var proj=localStorage.getItem("Bigmodel-Project")||"";'
        'var H={"Authorization":tok,"Bigmodel-Organization":org,"Bigmodel-Project":proj,"Accept":"application/json"};'
        f'var months={json.dumps(months)};'
        f'var start="{monday.isoformat()}",end="{sunday.isoformat()}";'
        'var kr=await fetch("/api/biz/v1/organization/"+org+"/projects/"+proj+"/api_keys?keyType=1",{headers:H});'
        'var kj=await kr.json();'
        'if(!kj.data){window.__glm_report=JSON.stringify({error:"KEYS_API:"+(kj.code||kr.status)+" "+(kj.msg||"")});return;}'
        'var nameById={};(kj.data||[]).forEach(function(x){nameById[x.apiKey]=x.name||"(未命名)";});'
        # 分页拉取：逐页直到累计行数达到 total，取不齐报错，绝不静默截断
        'var rows=[];'
        'for(var mi=0;mi<months.length;mi++){'
        'var total=-1,got=0,page=1;'
        'while(true){'
        'var u="/api/finance/expenseBill/expenseBillListByDay?billingMonth="+months[mi]'
        f'+"&billStatus=&modelProductName=&paymentType=&pageNum="+page+"&pageSize={BILL_PAGE_SIZE}";'
        'var br=await fetch(u,{headers:H});var bj=await br.json();'
        'if(!bj.rows){window.__glm_report=JSON.stringify({error:"BILL_API:"+(bj.code||br.status)+" "+(bj.msg||"")});return;}'
        'if(total<0){total=bj.total||0;}'
        'var rr=bj.rows||[];'
        'rr.forEach(function(x){if(x.billingDate>=start&&x.billingDate<=end)rows.push(x);});'
        'got+=rr.length;'
        'if(got>=total||rr.length===0)break;'
        'page++;'
        f'if(page>{BILL_MAX_PAGES}){{window.__glm_report=JSON.stringify({{error:"BILL_PAGE_OVERFLOW"}});return;}}'
        '}'
        'if(got<total){window.__glm_report=JSON.stringify({error:"BILL_TRUNCATED:"+got+"/"+total+"@"+months[mi]});return;}'
        '}'
        # apiUsage 按 (key,day,model) 取 max 再求和；tokenType 显式分类，未知类型单列
        'var byKDM={};'
        'rows.forEach(function(x){'
        'var k=(x.apiKey||"?")+"|"+x.billingDate+"|"+(x.modelCode||"?");'
        'var o=byKDM[k]=byKDM[k]||{key:x.apiKey,day:x.billingDate,inTok:0,outTok:0,otherTok:0,calls:0};'
        'var v=x.usageCount||0;'
        'if(x.tokenType==="输出"){o.outTok+=v;}'
        'else if(x.tokenType==="输入"){o.inTok+=v;}'
        'else if(x.tokenType==="缓存命中"){o.cacheTok=(o.cacheTok||0)+v;}'
        'else{o.otherTok+=v;}'
        'o.calls=Math.max(o.calls,x.apiUsage||0);});'
        'var byKD={};'
        'Object.keys(byKDM).forEach(function(k){'
        'var o=byKDM[k];var kk=o.key+"|"+o.day;'
        'var a=byKD[kk]=byKD[kk]||{key:o.key,day:o.day,inTok:0,outTok:0,cacheTok:0,otherTok:0,calls:0};'
        'a.inTok+=o.inTok;a.outTok+=o.outTok;a.cacheTok+=(o.cacheTok||0);a.otherTok+=o.otherTok;a.calls+=o.calls;});'
        'var billedDays={};rows.forEach(function(x){billedDays[x.billingDate]=1;});'
        'window.__glm_report=JSON.stringify({ok:true,nameById:nameById,rows:Object.values(byKD),billedDays:Object.keys(billedDays)});'
        '}catch(e){window.__glm_report=JSON.stringify({error:"JS:"+String(e).slice(0,200)});}})();'
        '"fired"'
    )
    raw, err = fire_and_poll(js, "window.__glm_report", tab_ref)
    if err:
        print(err)
        if err.startswith(("CHROME_PERM", "TAB_LOST", "OSA_ERR", "POLL_TIMEOUT")):
            sys.exit(3)
        sys.exit(4)
    try:
        payload = json.loads(raw)
    except Exception:
        print(f"BAD_JSON:{str(raw)[:200]}")
        sys.exit(4)
    if payload.get("error") == "NEED_LOGIN":
        print("NEED_LOGIN")
        sys.exit(2)
    if payload.get("error"):
        print(payload["error"])
        sys.exit(4)

    print(build_report(monday, sunday, payload))


if __name__ == "__main__":
    main()
