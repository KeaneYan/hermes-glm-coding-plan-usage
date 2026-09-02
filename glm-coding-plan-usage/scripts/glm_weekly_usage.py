#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GLM Coding Plan 按 key 用量报告生成器（周报 + 当日实时双模式）。

通过本机 Chrome 中已登录的智谱开放平台会话（cookie bigmodel_token_production），
调用控制台内部接口拉取「费用明细」（按 API Key 出账数据），聚合输出报告。

模式：
- 默认（周报）：按天接口 expenseBillListByDay，统计上一个完整周（周一~周日，
  或用 --monday 指定），T+1 出账口径。
- --today（当日实时）：按明细接口 expenseBillList（分钟级、当天可见），输出
  今天按 key 用量 + 当前 5h 窗口额度占比 + 周窗口进度。自动检测套餐版本
  （subscription/list 的 version 字段）并套用对应额度公式：
  * V2 旧版：加权 token = (输入+缓存命中+输出) × 模型系数 × 时段系数；
    glm-5.3 高峰×3/非高峰×1，glm-5.3-flash 高峰×1.2/非高峰×0.4；
    高峰=周一至五 14:00-18:00，周末全天非高峰。缓存命中 1:1 计入。
  * 新版积分制：积分=(入×6.9+缓存×1.7+出×24)/10000（glm-5.3）；
    flash 2.3/0.56/8；MCP 1.2/次；非高峰 50%。
  * 其他/未知版本：只报原始量并标注口径不确定。

聚合口径（与接口核实）：
- token 量 = 各行 usageCount 按 tokenType 显式分类：输入/输出/缓存命中三类分别
  累加；未知新类型单列 otherTok，报告中显式标注，不静默归入输入。
- 按天接口 apiUsage 在同一 (key, day, model) 多行重复：先按 (key,day,model)
  取 max 再对 (key,day) 求和。
- 按明细接口 apiUsage 在同一 (key, model, timeWindow) 多行重复（输入/输出行各
  带一次）：按该分组取 max 再求和。
- 分页返回 {total, rows[]}：逐页拉取并校验累计行数==total，取不齐报错退出，
  绝不输出缩水报告。
- 聚合一律以 apiKey 全量 ID 为键，名称仅用于展示（同名 key 附 ID 尾 4 位）。
- 费用折算 = Σ(usageCount × costPrice)（千token 单位除 1000，按次单位直乘），
  订阅行 settlementAmount=0，费用仅为刊例价折算。

退出码：0=成功；1=参数错误；2=需要重新登录（NEED_LOGIN）；3=Chrome/页面/权限
不可用；4=接口错误。
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

POLL_MAX = 40          # seconds（周报）
POLL_MAX_TODAY = 180   # --today 明细量大，放宽
BILL_PAGE_SIZE = 500
BILL_MAX_PAGES = 100   # safety bound, 100*500=50k rows/月远超实际

# V2 加权系数：modelCode -> (非高峰, 高峰)
V2_MULT = {"glm-5.3": (1.0, 3.0), "glm-5.3-flash": (0.4, 1.2)}
# 新版积分系数：modelCode -> (输入, 缓存命中, 输出)，结果除 10000
PTS_COEF = {"glm-5.3": (6.9, 1.7, 24.0), "glm-5.3-flash": (2.3, 0.56, 8.0)}
PTS_MCP_PER_CALL = 1.2   # MCP 工具按次计积分
PTS_OFFPEAK_DISCOUNT = 0.5


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
    if n >= 1e8:
        return f"{n/1e8:.2f}亿"
    if n >= 1e6:
        return f"{n/1e6:.1f}M"
    if n >= 1000:
        return f"{n/1e3:.1f}k"
    return str(round(n))


# ---------------------------------------------------------------- 周报模式

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
                 f"| {fmt_m(total_cache)} | {sum(v[2] for v in agg.values())} | 100% |")
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


def run_weekly(monday):
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


# ---------------------------------------------------------------- 当日实时模式

def run_today():
    ok, ref = ensure_bigmodel_tab()
    if not ok:
        print(ref)
        sys.exit(3)
    tab_ref = ref
    time.sleep(4)

    out, rc = tab_url(tab_ref)
    if rc == 0 and "/login" in out:
        print("NEED_LOGIN")
        sys.exit(2)

    js = (
        'window.__glm_today=null;'
        '(async function(){try{'
        'var tok=(document.cookie.match(/bigmodel_token_production=([^;]+)/)||[])[1];'
        'if(!tok){window.__glm_today=JSON.stringify({error:"NEED_LOGIN"});return;}'
        'var org=localStorage.getItem("Bigmodel-Organization")||"";'
        'var proj=localStorage.getItem("Bigmodel-Project")||"";'
        'var H={"Authorization":tok,"Bigmodel-Organization":org,"Bigmodel-Project":proj,"Accept":"application/json"};'
        # --- 基础信息：key 清单、套餐版本、配额 ---
        'var kr=await fetch("/api/biz/v1/organization/"+org+"/projects/"+proj+"/api_keys?keyType=1",{headers:H});'
        'var kj=await kr.json();'
        'if(!kj.data){window.__glm_today=JSON.stringify({error:"KEYS_API:"+(kj.code||kr.status)});return;}'
        'var nameById={};(kj.data||[]).forEach(function(x){nameById[x.apiKey]=x.name||"(未命名)";});'
        'var sr=await fetch("/api/biz/subscription/list",{headers:H});'
        'var sj=await sr.json();'
        # H1 修复：订阅接口失败必须报错退出（R6），不得静默落入某套公式
        'if(!sj.data){window.__glm_today=JSON.stringify({error:"SUB_API:"+(sj.code||sr.status)});return;}'
        'var plan=null;(sj.data||[]).forEach(function(s){if(s.status==="VALID")plan={productName:s.productName,version:s.version||null,billingCycle:s.billingCycle,purchaseTime:s.purchaseTime};});'
        'var qr=await fetch("/api/monitor/usage/quota/limit",{headers:H});'
        'var qj=await qr.json();'
        # M1 修复：配额接口失败必须报错退出（R6），不得静默吞掉窗口分析
        'if(!qj.data||!qj.data.limits){window.__glm_today=JSON.stringify({error:"QUOTA_API:"+(qj.code||qr.status)});return;}'
        'var limits=qj.data.limits;'
        # --- 由配额接口推窗口：5h 窗（unit=3）与周窗（unit=6）---
        'function fmt(d){function p(n){return (n<10?"0":"")+n;}'
        'return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds());}'
        'var now=new Date();'
        'var win5=null,winW=null,quotaNotes=[];'
        # M2 修复：nextResetTime 缺失/非法时跳过该窗口并记录说明，不得输出 "NaN-…" 垃圾
        'limits.forEach(function(l){'
        'if(l.type!=="TOKENS_LIMIT"||(l.unit!==3&&l.unit!==6))return;'
        'var ms=new Date(l.nextResetTime).getTime();'
        'if(isNaN(ms)){quotaNotes.push((l.unit===3?"5h":"周")+"窗口nextResetTime非法(" + String(l.nextResetTime) + ")，已跳过");return;}'
        'var pct=(typeof l.percentage==="number")?l.percentage:null;'
        'if(l.unit===3)win5={start:fmt(new Date(ms-5*3600*1000)),end:fmt(new Date(ms)),percentage:pct};'
        'if(l.unit===6)winW={start:fmt(new Date(ms-7*24*3600*1000)),end:fmt(new Date(ms)),percentage:pct};'
        '});'
        'var today=fmt(now).slice(0,10);'
        # 拉取覆盖窗口的月份明细
        'var mset={};mset[today.slice(0,7)]=1;'
        'if(win5)mset[win5.start.slice(0,7)]=1;'
        'if(winW)mset[winW.start.slice(0,7)]=1;'
        'var months=Object.keys(mset).sort();'
        'var rows=[];'
        'for(var mi=0;mi<months.length;mi++){'
        'var total=-1,got=0,page=1;'
        'while(true){'
        f'var u="/api/finance/expenseBill/expenseBillList?billingMonth="+months[mi]+"&pageNum="+page+"&pageSize={BILL_PAGE_SIZE}";'
        'var br=await fetch(u,{headers:H});var bj=await br.json();'
        'if(!bj.rows){window.__glm_today=JSON.stringify({error:"BILL_API:"+(bj.code||br.status)+"@"+months[mi]});return;}'
        'if(total<0)total=bj.total||0;'
        'var rr=bj.rows||[];'
        'rr.forEach(function(x){'
        # L2 修复：timeWindow 统一规范化（去 T/截 19 位）后再做字符串比较
        'var tw=(x.timeWindow||"").slice(0,19).replace("T"," ");'
        'if(x.billingDate===today||(win5&&tw>=win5.start)||(winW&&tw>=winW.start))rows.push(x);});'
        'got+=rr.length;'
        'if(got>=total||rr.length===0)break;'
        'page++;'
        f'if(page>{BILL_MAX_PAGES}){{window.__glm_today=JSON.stringify({{error:"BILL_PAGE_OVERFLOW"}});return;}}'
        '}'
        'if(got<total){window.__glm_today=JSON.stringify({error:"BILL_TRUNCATED:"+got+"/"+total+"@"+months[mi]});return;}'
        '}'
        # --- 加权计算（JS 侧，版本由 Python 传的公式参数决定？不——版本在 JS 里检测，公式常量写死 JS） ---
        f'var V2_MULT={json.dumps({k: list(v) for k, v in V2_MULT.items()})};'
        f'var PTS={json.dumps({k: list(v) for k, v in PTS_COEF.items()})};'
        f'var PTS_MCP={PTS_MCP_PER_CALL},PTS_OFFDIS={PTS_OFFPEAK_DISCOUNT};'
        'function isPeak(tw){try{var d=new Date(tw.slice(0,19).replace(" ","T"));'
        # L1 修复：timeWindow 缺失/非法时 Invalid Date 不抛异常，isNaN 守卫保守按高峰计（不得错误打折）
        'if(isNaN(d.getTime()))return true;'
        'var wd=d.getDay(),h=d.getHours();return wd>=1&&wd<=5&&h>=14&&h<18;}catch(e){return true;}}'
        # H1 修复：无 VALID 订阅或 version 缺失 => 版本未知(OTHER)只报原始量并标注；非 V2 值 => 新版积分制口径(标注假设)
        'var ver=!plan?"OTHER":(plan.version==="V2"?"V2":(plan.version?"NEW":"OTHER"));'
        'var unkM={},unkTT={};'  # M3/M4 修复：记录未识别模型与未知 tokenType，报告显式标注
        'function weight(x){'
        'var m=x.modelCode||"",tt=x.tokenType,v=x.usageCount||0,pk=isPeak(x.timeWindow||"");'
        'if(tt==="不区分输入输出"){'
        '  if(ver==="NEW")return v*PTS_MCP*(pk?1:PTS_OFFDIS);'
        '  return 0;'  # V2 的 MCP 系数未验证，单列不计
        '}'
        'if(ver==="V2"){var mm=V2_MULT[m];if(!mm){unkM[m]=1;return v;}return v*(pk?mm[1]:mm[0]);}'
        'if(ver==="NEW"){var c=PTS[m];if(!c){unkM[m]=1;return v;}var w=0;'
        'if(tt==="输入")w=v*c[0];else if(tt==="缓存命中")w=v*c[1];else if(tt==="输出")w=v*c[2];'
        'else{unkTT[String(tt)]=1;return v;}'  # M4 修复：NEW 版未知 tokenType 按原始量计入，不再静默权重为 0
        'return w/10000*(pk?1:PTS_OFFDIS);}'
        'return v;'  # OTHER/未知：原始量
        '}'
        # --- 聚合：今天（按 key 全量）、5h 窗口、周窗口 ---
        'var T={},W5={},WW={},maxTW="";'
        'var callSeen={};'  # 调用次数去重：(key|model|timeWindow) 取 max
        'rows.forEach(function(x){'
        'var k=x.apiKey||"?";var tw=(x.timeWindow||"").slice(0,19).replace("T"," ");var v=x.usageCount||0;var tt=x.tokenType;'  # L2：统一规范化
        'if(tw>maxTW)maxTW=tw;'
        'var w=weight(x);'
        'var cost=(x.costUnit==="次")?v*(x.costPrice||0):v/1000*(x.costPrice||0);'
        'if(x.billingDate===today){'
        'var a=T[k]=T[k]||{inTok:0,outTok:0,cacheTok:0,otherTok:0,calls:0,cost:0,w:0,mcp:0,models:{}};'
        'if(tt==="输出")a.outTok+=v;else if(tt==="输入")a.inTok+=v;else if(tt==="缓存命中")a.cacheTok+=v;'
        'else{a.otherTok+=v;if(tt==="不区分输入输出")a.mcp+=v;}'  # M4 修复：MCP 次数仅统计工具计费类型，未知类型只进 otherTok
        'a.cost+=cost;a.w+=w;if(x.modelCode)a.models[x.modelCode]=1;'
        'if(tt!=="不区分输入输出"){var gk=k+"|"+(x.modelCode||"")+"|"+tw;'
        'var prev=callSeen[gk]||0;var cur=x.apiUsage||0;'
        'if(cur>prev){a.calls+=cur-prev;callSeen[gk]=cur;}}'
        '}'
        'if(win5&&tw>=win5.start){var b=W5[k]=W5[k]||{w:0,raw:0};b.w+=w;b.raw+=v;}'
        'if(winW&&tw>=winW.start){var c2=WW[k]=WW[k]||{w:0};c2.w+=w;}'
        '});'
        'function simp(o){var r={};Object.keys(o).forEach(function(k){var a=o[k];'
        'r[k]={inTok:a.inTok,outTok:a.outTok,cacheTok:a.cacheTok,otherTok:a.otherTok,calls:a.calls,'
        'cost:Math.round(a.cost*10000)/10000,w:Math.round(a.w),mcp:a.mcp,models:Object.keys(a.models||{})};});return r;}'
        'function simpW(o){var r={};Object.keys(o).forEach(function(k){r[k]={w:Math.round(o[k].w),raw:o[k].raw||0};});return r;}'
        'window.__glm_today=JSON.stringify({ok:true,plan:plan,ver:ver,limits:limits,win5:win5,winW:winW,'
        'today:today,maxTW:maxTW,nameById:nameById,T:simp(T),W5:simpW(W5),WW:simpW(WW),'
        'quotaNotes:quotaNotes,unknownModels:Object.keys(unkM),unknownTT:Object.keys(unkTT)});'
        '}catch(e){window.__glm_today=JSON.stringify({error:"JS:"+String(e).slice(0,200)});}})();'
        '"fired"'
    )
    raw, err = fire_and_poll(js, "window.__glm_today", tab_ref, poll_max=POLL_MAX_TODAY)
    if err:
        print(err)
        if err.startswith(("CHROME_PERM", "TAB_LOST", "OSA_ERR", "POLL_TIMEOUT")):
            sys.exit(3)
        sys.exit(4)
    try:
        d = json.loads(raw)
    except Exception:
        print(f"BAD_JSON:{str(raw)[:200]}")
        sys.exit(4)
    if d.get("error") == "NEED_LOGIN":
        print("NEED_LOGIN")
        sys.exit(2)
    if d.get("error"):
        print(d["error"])
        sys.exit(4)

    print(build_today_report(d))


def build_today_report(d):
    name_by_id = d["nameById"]
    name_count = {}
    for nm in name_by_id.values():
        name_count[nm] = name_count.get(nm, 0) + 1

    def label(kid):
        nm = name_by_id.get(kid)
        if nm is None:
            return (kid or "?")[:12]
        if name_count.get(nm, 0) > 1:
            return f"{nm}(…{kid[-4:]})"
        return nm

    plan = d.get("plan") or {}
    plan_s = f"{plan.get('productName', '?')} {plan.get('version') or '新版/未知'}"
    ver = d["ver"]
    unit = {"V2": "加权token", "NEW": "积分", "OTHER": "原始token（口径未验证）"}[ver]

    lines = []
    lines.append(f"GLM Coding Plan 当日用量（{d['today']}，实时明细，数据截至 {d['maxTW'][11:16] or '—'}）")
    lines.append(f"套餐：{plan_s}｜额度口径：{unit}")
    for l in d.get("limits", []):
        # M2 修复：窗口被跳过（win5/winW 为 None）或 percentage 缺失时不输出垃圾/KeyError
        pct = l.get("percentage")
        pct_s = f"（官方已用 {pct}%）" if isinstance(pct, (int, float)) else ""
        if l.get("type") == "TOKENS_LIMIT" and l.get("unit") == 3 and d.get("win5"):
            lines.append(f"5h 窗口：{d['win5']['start'][11:16]} → {d['win5']['end'][11:16]}{pct_s}")
        if l.get("type") == "TOKENS_LIMIT" and l.get("unit") == 6 and d.get("winW"):
            lines.append(f"周窗口：{d['winW']['start'][:16]} → {d['winW']['end'][:16]}{pct_s}")
    lines.append("")

    # 今日总表
    T = d["T"]
    tot_w = sum(a["w"] for a in T.values())
    lines.append("| Key | 输入 | 输出 | 缓存命中 | 调用 | 费用折算¥ | 今日额度占比 |")
    lines.append("|---|---|---|---|---|---|---|")
    for k, a in sorted(T.items(), key=lambda kv: -kv[1]["w"]):
        share = a["w"] / tot_w * 100 if tot_w else 0
        mcp_s = f"+{a['mcp']}次MCP" if a["mcp"] else ""
        lines.append(f"| {label(k)} | {fmt_m(a['inTok'])} | {fmt_m(a['outTok'])} | {fmt_m(a['cacheTok'])} "
                     f"| {a['calls']}{mcp_s} | {a['cost']:.2f} | {share:.1f}% |")
    lines.append(f"| 合计 | {fmt_m(sum(a['inTok'] for a in T.values()))} | {fmt_m(sum(a['outTok'] for a in T.values()))} "
                 f"| {fmt_m(sum(a['cacheTok'] for a in T.values()))} | {sum(a['calls'] for a in T.values())} "
                 f"| {sum(a['cost'] for a in T.values()):.2f} | 100% |")

    # 5h 窗口
    W5 = d["W5"]
    if W5 and d.get("win5"):
        tot5 = sum(a["w"] for a in W5.values())
        pct5 = next((l.get("percentage") for l in d["limits"] if l.get("type") == "TOKENS_LIMIT" and l.get("unit") == 3), None)
        lines.append("")
        cap_s = ""
        if pct5:
            cap_s = f"｜隐含上限≈{fmt_m(tot5 / (pct5 / 100))} {unit}" if pct5 > 0 else ""
        lines.append(f"当前 5h 窗口按 key（{unit}{cap_s}）：")
        lines.append("| Key | 加权消耗 | 占比 |")
        lines.append("|---|---|---|")
        for k, a in sorted(W5.items(), key=lambda kv: -kv[1]["w"]):
            share = a["w"] / tot5 * 100 if tot5 else 0
            lines.append(f"| {label(k)} | {fmt_m(a['w'])} | {share:.1f}% |")
        lines.append(f"| 合计 | {fmt_m(tot5)} | 100% |")

    # 周窗口
    WW = d["WW"]
    if WW and d.get("winW"):
        totW = sum(a["w"] for a in WW.values())
        lines.append("")
        lines.append(f"周窗口累计：{fmt_m(totW)} {unit}")

    lines.append("")
    notes = ["口径：按明细实时接口（分钟级），账单较实时配额滞后约 45 分钟；"
             "费用为刊例价折算（订阅内实付 0）。"]
    if ver == "V2":
        notes.append("V2 加权=(入+缓存+出)×模型×时段系数（glm-5.3 高峰×3，flash 高峰×1.2，缓存 1:1 计入）；MCP 调用系数未验证，未计入加权。")
    elif ver == "NEW":
        notes.append("积分=(入×6.9+缓存×1.7+出×24)/10000（glm-5.3），非高峰 50%。")
        notes.append("version 字段为非 V2 值，按新版积分制口径折算（假设，未实测）。")
    else:
        notes.append("套餐版本未识别（无 VALID 订阅或 version 字段缺失），加权列为原始 token，仅供参考。")
    # M2/M3/M4 修复：脏数据与未识别项显式标注，不静默
    for qn in d.get("quotaNotes") or []:
        notes.append(str(qn) + "。")
    if d.get("unknownModels"):
        notes.append(f"未识别模型 {'、'.join(d['unknownModels'])}：未套用系数，按原始量计入加权，占比可能被低估。")
    if d.get("unknownTT"):
        notes.append(f"未知 tokenType {'、'.join(d['unknownTT'])}：已单列 other，加权按原始量计入。")
    lines.append("".join(notes))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="GLM Coding Plan 按 key 用量报告")
    ap.add_argument("--monday", help="周报周一 YYYY-MM-DD（默认上一个完整周）")
    ap.add_argument("--today", action="store_true", help="当日实时报告（按明细接口）+ 5h 窗口额度分析")
    args = ap.parse_args()

    if args.today:
        run_today()
        return

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
    run_weekly(monday)


if __name__ == "__main__":
    main()
