# coding: utf-8
"""
factor_lab_ui —— rxlib Streamlit 界面（唯一前端）。

只做"控件 -> cfg -> 调内核 limit_up_lab/factor_lab 编程接口 -> 展示"，
不复制撮合 / 指标 / 评估逻辑。数据源经 DataProvider 抽象访问。

本平台专用于**涨停打板**策略：历史回测（打板撮合）+ 单日区间当天选股。
"""
import os
import sys
import json
import datetime as dt

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '.pylibs'))
sys.path.insert(0, _HERE)

import pandas as pd
import streamlit as st

import factor_lab as fl
import limit_up_lab as lu
import rx_core as PB
import data_provider as dp
import local_settings as ls
import platform_env

st.set_page_config(page_title='rxlib 涨停打板平台', layout='wide',
                   page_icon=os.path.join(_HERE, 'favicon.ico'),
                   initial_sidebar_state='expanded')


# -------------------- 登录门 --------------------

import streamlit.components.v1 as components


def _set_cookie_js(name, value, days):
    return (
        '<script>(function(){'
        'var d=new Date();d.setTime(d.getTime()+%d*864e5);'
        'document.cookie="%s="+encodeURIComponent("%s")+";expires="'
        '+d.toUTCString()+";path=/;SameSite=Lax";'
        '})();</script>' % (days, name, value))


def _clear_cookie_js(name):
    return (
        '<script>(function(){'
        'document.cookie="%s=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/";'
        '})();</script>' % name)


def _logout_redirect_js(name):
    return (
        '<script>(function(){'
        'document.cookie="%s=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/";'
        'var w=0;'
        'var t=setInterval(function(){'
        'w++;'
        'if(!document.cookie.split(";").some(function(c){return c.trim().indexOf("%s=")===0;})||w>=10){'
        'clearInterval(t);'
        'window.location.replace(window.location.pathname);'
        '}'
        '},120);'
        '})();</script>' % (name, name))


MAX_LOGIN_FAILS = 5
LOGIN_LOCK_SECONDS = 300


def _login_until_ts():
    return st.session_state.get('_login_lock_until', 0)


def _login_locked():
    import time
    return _login_until_ts() > time.time()


def _login_lock_remain():
    import time
    return max(0, int(_login_until_ts() - time.time()))


def _do_login():
    pwd = st.session_state.get('_login_pwd', '')
    if ls.verify_password(pwd):
        st.session_state.logged_in = True
        st.session_state.pop('_login_error', None)
        st.session_state.pop('_login_fails', None)
        st.session_state.pop('_login_lock_until', None)
    else:
        import time
        fails = int(st.session_state.get('_login_fails', 0)) + 1
        st.session_state._login_fails = fails
        st.session_state._login_error = True
        if fails >= MAX_LOGIN_FAILS:
            st.session_state._login_lock_until = time.time() + LOGIN_LOCK_SECONDS


def _clear_sensitive_state():
    for k in ('_login_pwd', 'set_pwd_old', 'set_pwd1', 'set_pwd2',
              '_auth_token'):
        st.session_state.pop(k, None)
    for k in ('pool_spec', 'history'):
        st.session_state.pop(k, None)


def _do_logout():
    st.session_state.logged_in = False
    st.session_state._logout_done = True
    _clear_sensitive_state()
    try:
        get_panels.clear()
    except Exception:
        pass


def login_gate():
    is_logout = st.session_state.pop('_logout_done', False)
    if not is_logout and not st.session_state.get('logged_in'):
        token = st.context.cookies.get(ls.COOKIE_NAME)
        if token and ls.check_auth_token(token):
            st.session_state.logged_in = True
            st.session_state.pop('_login_fails', None)
            st.session_state.pop('_login_lock_until', None)
    if st.session_state.pop('_just_logged_in', False):
        components.html(
            _set_cookie_js(ls.COOKIE_NAME, st.session_state.pop('_auth_token'),
                           ls.COOKIE_DAYS), height=0)
    if not is_logout and st.session_state.get('logged_in'):
        return
    st.markdown(
        '<div style="max-width:380px;margin:70px auto;text-align:center">'
        '<h2>%s</h2><p>请输入访问密码</p></div>' % ls.load_settings()['title'],
        unsafe_allow_html=True)
    if is_logout:
        components.html(_logout_redirect_js(ls.COOKIE_NAME), height=0)
        st.success('已退出登录。')
    if _login_locked():
        st.error('连续失败次数过多，请 %d 秒后再试。' % _login_lock_remain())
        st.stop()
    if st.session_state.pop('_login_error', False):
        fails = int(st.session_state.get('_login_fails', 0))
        left = max(0, MAX_LOGIN_FAILS - fails)
        st.error('密码错误，请重试。还可尝试 %d 次。' % left)
    with st.form('login_form', clear_on_submit=True):
        st.text_input('访问密码', type='password', key='_login_pwd',
                      label_visibility='collapsed',
                      placeholder='请输入访问密码')
        st.form_submit_button('登 录', type='primary',
                              use_container_width=True, on_click=_after_click_login)
    st.caption('登录状态默认保留 %d 天；可随时在左侧栏退出。' % ls.COOKIE_DAYS)
    st.stop()


def _after_click_login():
    _do_login()
    if st.session_state.get('logged_in'):
        st.session_state._just_logged_in = True
        st.session_state._auth_token = ls.make_auth_token()


# -------------------- 面板缓存 --------------------

@st.cache_resource(max_entries=4, show_spinner=False)
def get_panels(tag, user_id, start, end, nproc, source, panel_version,
               pool_spec, disk_persist):
    provider = dp.get_provider(source, read_config=ls.get_db_config()) \
        if source == 'mysql' else dp.get_provider(source)
    cal, panels, degraded = fl.load_or_build(
        tag, user_id, pd.Timestamp(start), pd.Timestamp(end), nproc,
        read_config=ls.get_db_config(),
        boards=pool_spec.get('boards'),
        exclude_st=pool_spec.get('exclude_st', False),
        exclude_new_days=pool_spec.get('exclude_new_days'),
        custom_codes=pool_spec.get('custom_codes'),
        panel_version=panel_version, provider=provider,
        disk_persist=bool(disk_persist))
    return cal, panels, provider, degraded


@st.cache_resource(max_entries=4, ttl=120, show_spinner=False)
def get_intraday_panels(tag, user_id, start, nproc, source, panel_version,
                        pool_spec, snap_day):
    """盘中口径面板：历史日线 + 当天快照，重算全部指标（不写日线月片）。

    ttl=120：盘中 tick 每分钟变化会触发全量重算（约 95–145 秒），
    限制为最多 2 分钟重建一次，避免每次 Streamlit rerun 都重算。
    """
    provider = dp.get_provider(source, read_config=ls.get_db_config()) \
        if source == 'mysql' else dp.get_provider(source)
    cal, panels, iinfo = fl.load_or_build_intraday(
        tag, user_id, pd.Timestamp(start), nproc,
        read_config=ls.get_db_config(),
        boards=pool_spec.get('boards'),
        exclude_st=pool_spec.get('exclude_st', False),
        exclude_new_days=pool_spec.get('exclude_new_days'),
        custom_codes=pool_spec.get('custom_codes'),
        panel_version=panel_version, provider=provider,
        day=pd.Timestamp(snap_day))
    return cal, panels, provider, iinfo


@st.cache_data(ttl=300, show_spinner=False)
def latest_trade_date(source, _db_cfg):
    provider = dp.get_provider(source, read_config=_db_cfg) \
        if source == 'mysql' else dp.get_provider(source)
    return provider.latest_trade_date()


def _intraday_for_end(end, latest_close, probe):
    """所选结束日无盘后日线、但当天有盘中快照时，返回该快照日；否则 None。

    纯函数，便于单测：end <= latest_close 走日线且不调用盘中；
    end > latest_close 且当天存在盘中快照才启用盘中口径。
    """
    if latest_close is None or probe is None:
        return None
    end = pd.Timestamp(end)
    if end <= pd.Timestamp(latest_close):
        return None                      # 有盘后日线 -> 走日线，不调盘中
    snap = pd.Timestamp(probe['trade_date'])
    return snap if end == snap else None


@st.cache_data(ttl=60, show_spinner=False)
def api_meta():
    try:
        provider = dp.get_provider('api')
        return provider.meta() or {}
    except Exception:
        return {}


@st.cache_data(ttl=30, show_spinner=False)
def intraday_probe(source, _db_cfg):
    """探测该数据源最新可用的盘中快照。

    返回 {'trade_date': date, 'tick_time': str, 'n_snapshot': int}，无则 None。
    MySQL 直连与 API 模式均适用。
    """
    try:
        provider = dp.get_provider(source, read_config=_db_cfg) \
            if source == 'mysql' else dp.get_provider(source)
        latest = provider.intraday_latest()
        if latest is None:
            return None
        snap_df, snap_date, tick_time = provider.intraday_snapshot(latest)
    except Exception:
        return None
    if snap_df is None or snap_df.empty or snap_date is None:
        return None
    return {'trade_date': pd.Timestamp(snap_date).date(),
            'tick_time': tick_time,
            'n_snapshot': int(snap_df['rxcode'].nunique())}


def panels_or_message():
    s = st.session_state
    tag = s.tag
    # 判定：所选结束日无盘后日线、但当天有盘中快照 -> 自动改用盘中口径。
    # 有盘后日线时完全走日线，不调用盘中数据。
    try:
        latest_close = latest_trade_date(s.source, ls.get_db_config())
    except Exception:
        latest_close = None
    probe = None
    allow = (s.source != 'api') or bool(api_meta().get('allow_intraday', False))
    if allow and latest_close is not None \
            and pd.Timestamp(s.end) > pd.Timestamp(latest_close):
        probe = intraday_probe(s.source, ls.get_db_config())
    snap_day = _intraday_for_end(s.end, latest_close, probe)
    s.intraday_info = probe if snap_day is not None else None
    s.intraday_tag = None if snap_day is None else snap_day.strftime('%Y-%m-%d')
    try:
        with st.spinner('加载/构建面板（首次较慢，之后走缓存）...'):
            if snap_day is not None:
                cal, panels, provider, iinfo = get_intraday_panels(
                    tag, s.user_id, s.start, s.procs, s.source,
                    ls.get_panel_version(), s.pool_spec, snap_day)
                s.intraday_info = iinfo
                rx_info('本次为盘中口径：快照 %s %s，覆盖 %d 只；结果会随快照刷新。'
                        % (iinfo['trade_date'], iinfo['tick_time'] or '-',
                           iinfo['n_snapshot']))
                degraded = None
            else:
                cal, panels, provider, degraded = get_panels(
                    tag, s.user_id, s.start, s.end, s.procs, s.source,
                    ls.get_panel_version(), s.pool_spec,
                    platform_env.get_cache_config().get('panel_disk_cache',
                                                       False))
    except Exception as e:
        rx_error('面板取数失败：%s' % e)
        return None
    if degraded:
        st.markdown('<div class="rxlib-auth-warn">%s</div>' %
                    degraded, unsafe_allow_html=True)
    return cal, panels, provider, tag


# -------------------- 侧边栏：平台标题 + 参数 --------------------

def global_chrome_styles():
    st.markdown(
        '<style>'
        'header[data-testid="stHeader"] .st-emotion-cache-jma0kd.e1yxiy6j5,'
        'header[data-testid="stHeader"] [data-testid="stToolbar"] > div > div:first-child{'
        'display:flex;align-items:center;'
        'padding-left:2rem !important;padding-right:2rem !important;'
        '}'
        'header[data-testid="stHeader"] .st-emotion-cache-jma0kd.e1yxiy6j5::before,'
        'header[data-testid="stHeader"] [data-testid="stToolbar"] > div > div:first-child::before{'
        'content:"rxlib量化平台｜涨停板回测工具(limitup)";'
        'font-size:.95rem;font-weight:600;white-space:nowrap;'
        '}'
        '[data-testid="stMainBlockContainer"],'
        '.st-emotion-cache-zy6yx3{'
        'padding:2rem 1rem 10rem !important;'
        '}'
        # .st-emotion-cache-3n56ur：实测该元素 padding 为 1rem，收紧到 0.5rem
        # （emotion hash 类名随 Streamlit 构建版本变化，属易失选择器，升级后需复核）
        '.st-emotion-cache-3n56ur{'
        'padding:0.5rem !important;'
        '}'
        '[data-testid="stButton"] button,'
        '[data-testid="stDownloadButton"] button,'
        '[data-testid="stFormSubmitButton"] button{'
        'min-height:2rem !important;'
        '}'
        '.rx-run-btn p{'
        'font-size:0.75rem !important;'
        '}'
        '[data-testid="stSidebar"] h3{'
        'font-size:1rem;margin:.1rem 0 .54rem !important;padding:0 0 .05rem !important;'
        'line-height:1.28 !important;'
        '}'
        # 侧栏底部：「退出登录」下方默认有 6rem（sidebarTopSpace）内边距，收紧为 .3rem
        '[data-testid="stSidebar"] [data-testid="stSidebarUserContent"]{'
        'padding-bottom:.3rem !important;'
        '}'
        '[data-testid="stAppDeployButton"]{'
        'display:none !important;'
        '}'
        '[data-testid="stMainMenuItem-rerun"],'
        '[data-testid="stMainMenuItem-autoRerun"],'
        '[data-testid="stMainMenuItem-clearCache"],'
        '[data-testid="stMainMenuItem-print"],'
        '[data-testid="stMainMenuItem-recordScreencast"],'
        '[data-testid="stMainMenuItem-about"]{'
        'display:none !important;'
        '}'
        '[data-testid="stMainMenuList"] ~ div{'
        'display:none !important;'
        '}'
        '[data-testid="stMainMenuPopover"] [data-testid="stMainMenuDivider"]{'
        'display:none !important;'
        '}'
        '[data-testid="stThemeSwitcher"]{'
        'flex-direction:column !important;'
        '}'
        '[data-testid="stThemeSwitcher"] button{'
        'flex:0 0 auto !important;'
        'width:100%;'
        'flex-direction:row !important;'
        'justify-content:flex-start !important;'
        '}'
        '.rxlib-info{'
        'background-color:rgba(255, 188, 28, 0.1);'
        'color:rgb(242, 87, 70);'
        'font-size:14px;'
        'line-height:1.5;'
        'padding:0.5rem;'
        'border-radius:0.5rem;'
        'word-break:break-word;'
        '}'
        '.rxlib-info p{'
        'margin:0;'
        'color:rgb(242, 87, 70);'
        'font-size:14px;'
        '}'
        '.rxlib-error{'
        'background-color:rgba(242, 87, 70, 0.1);'
        'color:rgb(242, 87, 70);'
        'font-size:14px;'
        'line-height:1.5;'
        'padding:0.5rem;'
        'border-radius:0.5rem;'
        'word-break:break-word;'
        '}'
        '.rxlib-error p{'
        'margin:0;'
        'color:rgb(242, 87, 70);'
        'font-size:14px;'
        '}'
        '[data-testid="stMainBlockContainer"]:has(.rxlib-help-marker) h2{'
        'font-size:1.25rem;'
        '}'
        '[data-testid="stTabPanel"]:has(.rxlib-watch-marker) h3{'
        'font-size:1.2rem;'
        '}'
        '.rxlib-auth-warn{'
        'margin:.2rem 0 .35rem;padding:.28rem .6rem;'
        'font-size:.8rem;line-height:1.3;border-radius:.35rem;'
        'background:rgba(255,193,7,.14);'
        'border:1px solid rgba(255,193,7,.55);'
        'color:inherit;'
        '}'
        '.rxlib-auth-warn p{margin:0;font-size:.8rem;}'
        # 专项策略预设提示：小字号、红字、浅红底、紧凑行距（浅/深色主题均可读）
        '.rxlib-preset-hint{'
        'background:rgba(198,40,40,.10);'
        'color:#c62828;'
        'font-size:.8rem;'
        'line-height:1.35;'
        'padding:.22rem .55rem;'
        'margin:.2rem 0 .75rem;'
        'border-left:3px solid #c62828;'
        'border-radius:.3rem;'
        'word-break:break-word;'
        '}'
        '.rxlib-preset-hint p{'
        'margin:0 0 .75rem;color:#c62828;font-size:.8rem;line-height:1.35;'
        '}'
        '.rxlib-preset-hint b{color:#b71c1c;font-weight:600;}'
        # 侧栏：收紧菜单项（控件）之间的上下间距与行高，保留可点击热区
        '[data-testid="stSidebar"] [data-testid="stVerticalBlock"]{'
        'gap:.4rem !important;'
        '}'
        '[data-testid="stSidebar"] [data-testid="stWidgetLabel"]{'
        'margin-bottom:.05rem !important;'
        '}'
        '[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p{'
        'font-size:.8rem !important;line-height:1.2 !important;'
        '}'
        '[data-testid="stSidebar"] [role="radiogroup"]{'
        'gap:.05rem !important;'
        '}'
        '[data-testid="stSidebar"] [role="radiogroup"] label{'
        'padding:.12rem 0 !important;min-height:1.7rem;'
        '}'
        # 侧栏输入类控件：整体略矮（文本框/数字/下拉/日期），不影响输入可读性
        '[data-testid="stSidebar"] [data-baseweb="input"],'
        '[data-testid="stSidebar"] [data-testid="stNumberInputContainer"],'
        '[data-testid="stSidebar"] [data-baseweb="select"] > div{'
        'min-height:2.05rem !important;'
        '}'
        '[data-testid="stSidebar"] [data-baseweb="input"] input,'
        '[data-testid="stSidebar"] [data-testid="stNumberInputContainer"] input{'
        'font-size:.85rem !important;padding-top:.1rem !important;padding-bottom:.1rem !important;'
        '}'
        '[data-testid="stSidebar"] [data-baseweb="select"] div[role="button"]{'
        'font-size:.85rem !important;padding-top:.1rem !important;padding-bottom:.1rem !important;'
        '}'
        '</style>', unsafe_allow_html=True)


def mark_run_buttons():
    components.html(
        '<script>(function(){'
        'try{var doc=window.parent.document;}catch(e){return;}'
        'function tagAll(){'
        'var bs=doc.querySelectorAll("[data-testid=\\"stButton\\"] button");'
        'for(var i=0;i<bs.length;i++){'
        'var t=(bs[i].textContent||"").trim();'
        'if(t.charAt(0)==="▶"){bs[i].classList.add("rx-run-btn");}'
        'else{bs[i].classList.remove("rx-run-btn");}'
        '}'
        '}'
        'tagAll();'
        'var mo=new MutationObserver(function(){tagAll();});'
        'mo.observe(doc.body,{childList:true,subtree:true});'
        '})();</script>', height=0)


def rx_info(text):
    safe = str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    st.markdown('<div class="rxlib-info">%s</div>' % safe, unsafe_allow_html=True)

def rx_error(text):
    safe = str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    st.markdown('<div class="rxlib-error">%s</div>' % safe, unsafe_allow_html=True)


def preset_lock_hint(strategy_name, lock_desc):
    """专项策略预设提示（小字号红字紧凑样式，所有专项策略预设统一复用）。"""
    safe_name = str(strategy_name).replace('&', '&amp;').replace('<', '&lt;') \
        .replace('>', '&gt;')
    safe_desc = str(lock_desc).replace('&', '&amp;').replace('<', '&lt;') \
        .replace('>', '&gt;')
    st.markdown(
        '<div class="rxlib-preset-hint">当前为专项策略「<b>%s</b>」，'
        '使用脚本固化口径（%s）。'
        '下方买入模式、入场/出场、板块共振等参数均不生效；'
        '股票池、回测区间与费率可调，先过滤股票池再计算题材强度与排名。</div>'
        % (safe_name, safe_desc),
        unsafe_allow_html=True)


def brand_header():
    title = str(ls.load_settings()['title'])
    safe = title.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')
    st.markdown(
        '<style>'
        '[data-testid="stSidebarHeader"]::before{'
        'content:"%s";'
        'font-size:1.2rem;font-weight:700;'
        'color:#D90000;opacity:.92;'
        'padding-left:.35rem;min-width:0;'
        'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'
        'align-self:center;'
        '}'
        '</style>' % safe, unsafe_allow_html=True)


def sidebar_controls():
    with st.sidebar:
        st.subheader('数据源')
        source = st.radio('数据提供层', ['mysql', 'api'], index=1,
                          format_func=lambda x: '直连 MySQL' if x == 'mysql'
                          else 'API', horizontal=True,
                          help='选择回测和基准数据的访问方式。')
        if source == 'api':
            _api = platform_env.get_api_config()
            if not (_api.get('base_url') and _api.get('token')):
                st.caption('请在「系统设置」中填写 API 地址和访问令牌。')

        st.subheader('股票池 / 区间')
        pool, pool_spec = pool_controls()
        c1, c2 = st.columns(2)
        start = c1.date_input('开始日期', value=dt.date(2026, 3, 1),
                              help='回测起始交易日，窗口会自动向前取约 2 个月数据用于指标预热；'
                                   '选单日（开始=结束）即当天打板选股。')
        try:
            default_end = latest_trade_date(source, ls.get_db_config())
        except Exception:
            default_end = dt.date(2026, 9, 18)
        end = c2.date_input('结束日期', value=default_end,
                            help='回测结束交易日，默认自动取库内最新交易日。')
        procs = st.number_input('并行进程数', min_value=1, max_value=32,
                                value=max(1, (os.cpu_count() or 6) - 2),
                                help='构建面板时的 CPU 进程数；机器核多可调大以加快首次建库。')

        tag = build_tag(pool, pool_spec, start, end)

        st.subheader('打板策略参数')
        limit_cfg = limit_sidebar_controls()

        st.subheader('其它')
        fee_bps = st.number_input('单边费率（bp，万分之3=3）', min_value=0.0,
                                  max_value=100.0, value=0.0, step=0.5,
                                  help='买入和卖出各收取一次的交易费率，单位为基点（1bp=0.01%）。默认0表示不计费。')

        st.divider()
        st.button('退出登录', on_click=_do_logout, use_container_width=True)

    st.session_state.update(
        user_id=pool_spec.get('user_id'), start=pd.Timestamp(start),
        end=pd.Timestamp(end), procs=int(procs), source=source,
        limit_cfg=limit_cfg,
        fee_bps=float(fee_bps), pool_label=pool, pool_spec=pool_spec, tag=tag)


def limit_sidebar_controls():
    """涨停打板回测侧栏：保留打板核心可用参数，去掉趋势专用无用项，并增加板块涨幅选项。"""
    presets = {k: v for k, v in lu.limit_preset_cfgs().items()
               if not k.startswith(('动态套用挖掘库最优', '共振≥3 + 金叉'))}
#   st.subheader('打板预设')
    preset = st.selectbox('预设组合', ['（不载入，用下方自定义）'] +
                          sorted(presets.keys()), index=1,
                          help='载入预设组合的整套参数；专项策略（带 _strategy）'
                               '走独立固化引擎，下方参数不参与。')
    base = lu.limit_base_cfg()
    if preset != '（不载入，用下方自定义）':
        base.update(presets[preset])

    # 专项策略（带 _strategy）走独立固化引擎，下列入场/出场/板块参数均不参与；
    # 上方股票池、回测区间与费率生效，故仅将固化参数置灰并提示。
    locked = bool(base.get('_strategy'))
    if locked:
        if base.get('_strategy') == 'sector_watch_t1':
            _lock_desc = 'T日盘后照盯盘清单排序，T+1封板且非一字才以涨停价扫入、断板即出'
        else:
            _lock_desc = '当日涨停价买、一字板跳过、断板即出'
        preset_lock_hint(preset.split('/')[0], _lock_desc)

    _buy_modes = {
        'low_first': '低位首板（低吸）',
        'first_limit': '首板（当日涨停）',
        'strength': '高封板强度首板（胜率优先）',
        'limit_count_ge': '连板高度≥N',
        'yest_relay': '连板接力（昨连板+今）',
    }
    buy_mode = st.selectbox(
        '买入模式', list(_buy_modes), index=list(_buy_modes).index(base.get('buy_mode', 'low_first')),
        format_func=lambda x: _buy_modes.get(x, x), disabled=locked,
        help='打板入场模式。')
    _timings = {'limit_close': '当日涨停收盘', 'next_open': '次日开盘追板',
                'next_close': '次日收盘追板'}
    buy_timing = st.selectbox(
        '买入时点', list(_timings), index=list(_timings).index(base.get('buy_timing', 'next_open')),
        format_func=lambda x: _timings.get(x, x), disabled=locked,
        help='打板回测对无法精确还原盘中排队的近似成交口径，默认推荐次日开盘。')
    topk = st.number_input('每日最多买入 topK', min_value=1, max_value=100,
                           value=int(base.get('topk', 10)), disabled=locked,
                           help='符合条件的标的最多买入家数。')

    with st.expander('连板 / 封板条件', expanded=True):
        min_limit = base.get('min_limit_count', 2)
        _ml = st.number_input('连板高度下限（N）', min_value=1, max_value=20,
                              value=int(min_limit),
                              disabled=locked or buy_mode != 'limit_count_ge',
                              help='buy_mode=连板高度≥N 时的连板数阈值。')
        min_relay = st.number_input('接力昨日连板数下限', min_value=1, max_value=20,
                                    value=int(base.get('min_relay_count', 2)),
                                    disabled=locked or buy_mode != 'yest_relay',
                                    help='buy_mode=连板接力 时的昨日连板数阈值。')
        prev_req = st.number_input('近 N 日有涨停（0=关闭）', min_value=0, max_value=60,
                                   value=int(base.get('prev_up_req') or 0),
                                   disabled=locked,
                                   help='近 N 个交易日内出现过涨停才保留；0 表示禁用该附加条件。')
        low_hi_mult = st.slider('高位阈值（低吸用 low_hi_mult）', 0.5, 1.0,
                                float(base.get('low_hi_mult', 0.85)), step=0.01,
                                disabled=locked,
                                help='hl 判为低位的成本位分位阈值，仅 low_first 模式使用。')
        strength_vol = st.slider('封板强度量比阈值', 0.2, 2.0,
                                 float(base.get('strength_vol', 0.5)), step=0.05,
                                 disabled=locked,
                                 help='strength 强板判定的量比阈值上限。')

    with st.expander('出场条件', expanded=False):
        stop_loss = pct_input('止损', base.get('stop_loss'), disabled=locked,
                              help='相对买入价最大亏损，触发当日按理论价成交；空=关闭。')
        take_profit = pct_input('止盈', base.get('take_profit'), disabled=locked,
                                help='相对买入价盈利目标，触发当日按理论价成交；空=关闭。')
        max_hold = st.number_input('最大持有交易日（0=不限）', min_value=0, max_value=250,
                                   value=int(base.get('max_hold') or 0), disabled=locked,
                                   help='持有达到该交易日后到期收盘卖出。')
        plan_sell = st.checkbox('断板即出（plan_sell）', value=bool(base.get('plan_sell', True)),
                                disabled=locked,
                                help='次日未继续封板即按收盘价离场；这是打板稳健离场的核心开关。')
        breakout_sell = st.checkbox('炸板即出（breakout_sell）', value=bool(base.get('breakout_sell', False)),
                                    disabled=locked,
                                    help='盘中冲高回落不再封板即离场；与断板即出可同时开启。')

    with st.expander('板块共振 + 板块涨幅', expanded=False):
        st.caption('当日该股所属板块涨停家数/涨幅落在区间内才可打板。'
                   '面板列 board_upcount / board_upcount_touch / board_ret '
                   '由 build_board_resonance 挂载；DB 不可用时自动失效。')
        reson_req = st.slider('板块涨停家数下限（0=关闭）', 0, 20,
                              int(base.get('reson_req', 0)), disabled=locked,
                              help='所属板块当日涨停家数 >= 该值才保留；0 表示禁用。')
        _rmax = base.get('reson_max')
        _rmax_num = int(_rmax) if _rmax else 20
        reson_max = st.slider('板块涨停家数上限（同下限=不限）', 0, 50, _rmax_num,
                              disabled=locked,
                              help='用于过滤过热板块；设为与下限相同或 0 表示不限。')
        if reson_max and reson_max <= reson_req:
            reson_max = None
        if base.get('reson_count') is None or str(base.get('reson_count', 'seal')).startswith('seal'):
            _ri = 0
        else:
            _ri = 1
        reson_count = st.radio(
            '涨停口径', ['seal', 'touch'], index=_ri,
            format_func=lambda x: 'seal · 收盘封板' if x == 'seal' else 'touch · 含当日炸板',
            horizontal=True, disabled=locked,
            help='seal 只看收盘仍封板；touch 把盘中曾触板也计入（打板更贴合）。')
        board_ret_req = st.slider(
            '板块涨幅下限（%，0=关闭）', 0.0, 20.0,
            float(base.get('board_ret_req', 0)) * 100,
            step=0.5, disabled=locked,
            help='所属板块当日等权涨幅 >= 该值才保留；0 表示禁用。'
                 '与涨停家数条件可同时启用，也可单独使用。') / 100.0
        concept_type = st.radio(
            '板块层级', ['TGN 概念', 'THY1 一级', 'THY2 二级', 'THY3 三级'],
            index=int(base.get('concept_type', 3)),
            format_func=lambda x: x.split(' ', 1)[1],
            horizontal=True, disabled=locked,
            help='按哪个概念层级聚合板块涨停家数/涨幅，默认 THY3 三级行业。')

    cfg = lu.limit_base_cfg(
        buy_mode=buy_mode, buy_timing=buy_timing, topk=int(topk),
        min_limit_count=int(min_limit), min_relay_count=int(min_relay),
        prev_up_req=(int(prev_req) if prev_req > 0 else 0),
        low_hi_mult=float(low_hi_mult), strength_vol=float(strength_vol),
        stop_loss=stop_loss, take_profit=take_profit,
        max_hold=(int(max_hold) if max_hold else None),
        plan_sell=bool(plan_sell), breakout_sell=bool(breakout_sell),
        reson_req=int(reson_req), reson_max=reson_max, reson_count=reson_count,
        board_ret_req=float(board_ret_req),
        concept_type=concept_type,
        buy_cross=False, buy_bars=1, buy_sens='std')
    # 专项策略（强势板块强势股·情景A / 近10日涨停≥2 / 盯盘T+1）走独立固化引擎，
    # 需把 _strategy 标记及其固化参数透传到 limit_cfg
    if base.get('_strategy'):
        cfg['_strategy'] = base['_strategy']
        cfg['_label'] = base.get('_label', '')
        for _k in ('buy_scope', 'top_sectors', 'top_leaders'):
            if _k in base:
                cfg[_k] = base[_k]
    return cfg


def pool_controls():
    choices = ['测试股票池（%d只）' % len(fl.TEST_POOL_CODES)]
    for w in ls.list_watchlists():
        choices.append('自定义 · %s' % w)
    for b in fl.BOARD_ORDER:
        choices.append('板块 · %s' % b)
    choices.append('全 A（全市场）')
    pool = st.selectbox('股票池', choices, index=0,
                        help='选择回测/选股的标的范围；默认为测试股票池，自定义池在"自选池管理"页维护。')

    pool_spec = {'user_id': None, 'boards': None, 'exclude_st': False,
                 'exclude_new_days': None, 'custom_codes': None}
    if pool.startswith('测试股票池'):
        pool_spec['custom_codes'] = fl.test_pool_rxcodes()
    elif pool.startswith('板块 · '):
        pool_spec['boards'] = [pool.split('·', 1)[1].strip()]
    elif pool.startswith('自定义 · '):
        name = pool.split('·', 1)[1].strip()
        wl = ls.load_watchlist(name)
        pool_spec['custom_codes'] = wl['rxcode'].tolist()

    exclude_st = st.checkbox('排除 ST 股', value=False,
                             help='在所选股票池基础上剔除所有 ST/*ST 风险警示股票。')
    exclude_new = st.checkbox('排除次新股', value=False,
                              help='剔除上市未满下方天数的股票。')
    new_days = st.number_input('次新股上市天数门槛', min_value=1, max_value=1000,
                               value=fl.NEW_LISTING_DAYS, disabled=not exclude_new,
                               help='上市自然日数小于该值的股票会被剔除，默认90天。')
    pool_spec['exclude_st'] = exclude_st
    pool_spec['exclude_new_days'] = int(new_days) if exclude_new else None
    return pool, pool_spec


def build_tag(pool, pool_spec, start, end):
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    if pool_spec.get('custom_codes') is not None:
        codes = pool_spec['custom_codes']
        import zlib
        key = 'wl%x' % (zlib.crc32(','.join(sorted(codes)).encode('utf-8')) & 0xffffffff)
    elif pool_spec.get('boards'):
        key = 'b' + pool_spec['boards'][0][:2]
    else:
        key = 'mkt'
    sfx = ''
    if pool_spec.get('exclude_st'):
        sfx += 's'
    if pool_spec.get('exclude_new_days'):
        sfx += 'n%d' % pool_spec['exclude_new_days']
    return '%s_%s_%s%s' % (key, s.strftime('%m%d'), e.strftime('%m%d'), sfx)


def pct_input(label, val, help=None, disabled=False):
    v = st.number_input('%s（%%，0=关闭）' % label, min_value=0.0, max_value=100.0,
                        value=round(float(val) * 100, 1) if val is not None else 0.0,
                        step=0.5, help=help, disabled=disabled)
    return (v / 100.0) if v > 0 else None


# -------------------- 可溯源：参数快照 --------------------

def snapshot(cfg, tag):
    s = st.session_state
    snap = {'run_time': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'pool': s.pool_label, 'user_id': s.user_id,
            'start': str(s.start.date()), 'end': str(s.end.date()),
            'source': s.source, 'fee_bps': s.fee_bps, 'cache_tag': tag,
            'cfg': cfg}
    st.session_state.setdefault('history', []).append(snap)
    return snap


def show_snapshot(snap, key='snap'):
    with st.expander('本次运行参数快照（可溯源）'):
        st.json(snap)
        st.download_button('下载参数快照 JSON',
                           data=json.dumps(snap, ensure_ascii=False, indent=2,
                                           default=str),
                           file_name='factor_lab_snapshot_%s.json'
                           % dt.datetime.now().strftime('%Y%m%d_%H%M%S'),
                           key='dl_snapshot_%s' % key)


def limit_signature():
    s = st.session_state
    return json.dumps(
        [str(s.tag), str(s.start.date()), str(s.end.date()),
         s.user_id, s.source, s.fee_bps, s.get('limit_cfg')],
        ensure_ascii=False, default=str, sort_keys=True)


# -------------------- Tab1 历史回测（涨停打板） --------------------

def tab_limit(state):
    """历史回测（涨停打板）：复用面板，用 limit_up_lab 内核撮合，展示打板专属指标。

    单日区间（开始=结束）等价于当天打板选股：交易明细即当天选中的股票，
    未卖出的持仓（exit_reason=open）按最新收盘估值，浮盈计入净值。
    """
    cal, panels, provider, tag = state
    st.button('▶ 运行历史回测', type='primary', key='run_limit')
    s = st.session_state
    if s.get('run_limit'):
        strat = (s.limit_cfg or {}).get('_strategy')
        if strat == 'sector_watch_t1':
            # 专项策略：「强势板块」T+1 人工操作法（无前视，独立引擎，直接连库）
            with st.spinner('专项策略回测计算中（盯盘T+1·%s）...'
                            % (s.limit_cfg or {}).get('buy_scope', 'leader')):
                res = lu.strategy_backtest_watch(
                    start=s.start, end=s.end, fee_bps=s.fee_bps,
                    top_sectors=int((s.limit_cfg or {}).get('top_sectors', 3)),
                    top_leaders=int((s.limit_cfg or {}).get('top_leaders', 3)),
                    buy_scope=(s.limit_cfg or {}).get('buy_scope', 'leader'),
                    pool_spec=s.pool_spec)
        elif strat:
            # 专项策略：强势板块强势股·情景A / 近10日涨停≥2（独立引擎，直接连库）
            with st.spinner('专项策略回测计算中（%s）...' % strat):
                res = lu.strategy_backtest_once(
                    strategy=strat, start=s.start, end=s.end, fee_bps=s.fee_bps,
                    pool_spec=s.pool_spec)
        else:
            with st.spinner('历史回测（涨停打板）计算中（复用已有面板）...'):
                res = lu.limit_backtest_once(
                    s.limit_cfg, s.start, s.end, user_id=s.user_id,
                    fee_bps=s.fee_bps, provider=provider,
                    panels_state=(cal, panels), do_mining=False)
        s.last_limit = res
        s.last_limit_tag = tag
        s.last_limit_sig = limit_signature()
        s.last_limit_snap = snapshot(res['cfg'], tag)
    res = s.get('last_limit')
    if res is None:
        rx_info('在左侧设置打板参数后点击运行。')
        return
    tag = s.last_limit_tag
    if s.get('last_limit_sig') != limit_signature():
        rx_info('左侧参数已修改，当前为上次运行结果；点击运行以更新。')
    m = res['metrics']
    if s.start.date() == s.end.date():
        st.info('单日区间 %s：仅对当天选中的股票开仓；未卖出的持仓（状态=持仓中）按最新收盘估值显示浮盈。'
                % s.start.date())

    c = st.columns(5)
    c[0].metric('区间收益', '%.2f%%' % m['final'])
    c[1].metric('区间峰值', '%.2f%%' % m['peak'])
    c[2].metric('最大回撤', '%.2f%%' % m['mdd'])
    c[3].metric('胜率', '%.1f%%' % m['win_rate'] if m.get('n_trades') else '-')
    c[4].metric('盈亏比 PF', fmt(m['pf']))
    c = st.columns(5)
    c[0].metric('交易笔数', m.get('n_trades', 0))
    c[1].metric('触发家数 n_buys', m.get('n_buys', 0))
    c[2].metric('未平仓', m.get('n_open', 0))
    c[3].metric('平均持有(交易日)', fmt(m.get('med_hold')))
    if 'n_skip' in m:
        c[4].metric('空仓日数 n_skip', m.get('n_skip', 0),
                    help='有盯盘清单但 T+1 无候选成交（未封板/一字排不进）而空仓的交易日数。')
    else:
        c[4].metric('板块涨幅下限',
                    '%.1f%%' % (float(res['cfg'].get('board_ret_req') or 0) * 100)
                    if res['cfg'].get('board_ret_req') else '关闭')

    eq = res['equity']
    if eq is not None and len(eq):
        import plotly.graph_objects as go
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=eq['date'], y=eq['equity'], name='打板净值'))
        fig.update_layout(height=420, hovermode='x unified', yaxis_title='净值')
        st.plotly_chart(fig, use_container_width=True)

        dd = eq['equity'] / eq['equity'].cummax() - 1.0
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=eq['date'], y=dd * 100, name='回撤%',
                                  fill='tozeroy', line=dict(color='crimson')))
        fig2.update_layout(height=240, yaxis_title='回撤%')
        st.plotly_chart(fig2, use_container_width=True)

    tr = res['trades']
    st.markdown('**交易明细**')
    st.caption('`状态=持仓中`（exit_reason=open）为区间结束时仍未卖出的仓位，按最新收盘价'
               '估值；`浮盈(%)` 即其浮动盈亏（已计入净值，不计入胜率/盈亏比/交易数）。'
               '选单日区间时，本明细即当天选中的股票名单。')
    if len(tr):
        tr = tr.reset_index(drop=True)
        tr = tr.drop(columns=['rxcode'], errors='ignore')
        if 'sector_title' in tr.columns:
            tr['sector_title'] = (tr['sector_title'].astype(str)
                                  .str.replace('THY3·', '', regex=False))
        tr.insert(1, '状态', tr['exit_reason'].map(
            lambda r: '持仓中' if r == 'open' else '已平仓'))
        tr.insert(len(tr.columns) - 1, '浮盈(%)', (tr['ret_pct'] * 100).round(2))
        tr.index = tr.index + 1
        tr.index.name = '序号'
    st.dataframe(tr, use_container_width=True)
    _td1, _td2 = st.columns([1, 1], gap="small")
    with _td1:
        st.download_button('下载交易明细（UTF-8）',
                           data=_csv_fix_codes(tr, excel=True).to_csv(
                               index=bool(len(tr)), encoding='utf-8-sig'),
            file_name='limit_lab_trades_%s.csv' % tag,
            key='dl_limit_trades_utf8', use_container_width=True)
    with _td2:
        st.download_button('下载交易明细（GBK）',
                           data=_csv_fix_codes(tr, excel=True).to_csv(
                               index=bool(len(tr))).encode(
                               'gb18030', errors='replace'),
                           file_name='limit_lab_trades_%s_gbk.csv' % tag,
                           key='dl_limit_trades_gbk', use_container_width=True,
                           help='若用 UTF-8 版本打开中文乱码，请下载此版本。')
    show_snapshot(s.last_limit_snap, key='limit')


# -------------------- Tab 今日复盘·近10日涨停≥2（T-1 盘后） --------------------

_WATCH_BOARD_MAP = {'one_word': '一字板', 't_board': 'T字板',
                    'turnover': '换手板', 'not_lu': '未封板', 'unknown': '未知'}


def tab_watch_pool(state):
    """今日复盘·近10日涨停≥2，独立连库，不依赖 panels，容忍 state=None。"""
    st.subheader('🔭 近10日涨停≥2')
    

    s = st.session_state
    default_date = s.get('end').date() if s.get('end') is not None else None

    c1, c2 = st.columns(2)
    base_date = c1.date_input('复盘日期（当日已收盘）', value=default_date,
                              key='review_lu10_base_date',
                              help='默认取侧栏结束日（库内最新交易日）。')
    tiers = c2.select_slider('连板梯队筛选（0=不限）', options=[0, 1, 2, 3], value=0,
                             key='review_lu10_tiers',
                             format_func=lambda x: '不限（全部）' if x == 0 else '前 %d 档' % x,
                             help='0=不限：显示近 N 日涨停达标的全部个股，与回测统计口径一致。'
                                  '历史回测样本中约 8 成不在复盘日最高梯队，筛选高档位时显示范围会明显缩小；'
                                  '设为 1/2/3 仅按连板高度前 N 档筛选显示，不改变统计口径。')
    c3, c4, c5 = st.columns(3)
    lookback_n = c3.number_input('近 N 个交易日', min_value=1, max_value=30, value=10,
                                 key='review_lu10_lookback_n')
    min_count = c4.number_input('涨停次数不少于', min_value=1, max_value=30, value=2,
                                key='review_lu10_min_count')
    hide_one_word = c5.checkbox(
        '隐藏复盘日一字板', value=False, key='review_lu10_hide_one_word',
        help='仅作展示筛选；勾选后清单与回测统计口径不再一致，如需对照回测请保持不勾选。')
    relay_only = st.checkbox(
        '仅显示复盘日收盘封板的个股（缩小范围）', value=False,
        key='review_lu10_relay_only',
        help='只保留复盘日收盘仍封板的个股（换手板 / T字板 / 一字板），剔除当日未封板者。'
             '历史回测 63 个已平仓样本在该口径下无一遗漏；但筛选后不再覆盖完整统计口径，'
             '极端行情下个别样本形态（如当日未封板、次日反包）会被排除。'
             '默认关闭以保持完整口径。')

    st.button('🔭 生成复盘清单', type='primary', key='review_lu10_run')
    if s.get('review_lu10_run'):
        with st.spinner('正在生成复盘清单...'):
            res = lu.build_lu10_watchlist(
                base_date=base_date, tiers=tiers, lookback_n=int(lookback_n),
                min_count=int(min_count), exclude_one_word=bool(hide_one_word),
                relay_only=bool(relay_only))
        s.last_watch_pool = res

    res = s.get('last_watch_pool')
    if res is None:
        rx_info('设置参数后点击「生成复盘清单」。')
        return
    if res.get('error'):
        st.warning(res['error'])
        return

    base = pd.Timestamp(res['base_date']).strftime('%Y-%m-%d')
    nxt = res['next_date']
    intraday = res.get('intraday')
    is_intra = bool(intraday and intraday.get('is_intraday'))
    nxt_s = ('待定（盘中）' if is_intra and nxt is None
             else pd.Timestamp(nxt).strftime('%Y-%m-%d') if nxt is not None
             else '待定')
    cand = res['candidates']
    if is_intra:
        _tick_s = pd.Timestamp(intraday['tick_time']).strftime('%Y-%m-%d %H:%M:%S')
        st.warning('⚡ **本次为盘中口径：快照 %s**　|　复盘日日线尚未生成，'
                   '以下清单由「截至前一交易日日线 + 当日最新快照」合成：'
                   '近 %d 日窗口含当日、连板/板型按快照现价封板近似、**首封时间缺失**、'
                   '下一交易日未定。快照随盘中刷新变化，**收盘后请用盘后口径重新生成**。'
                   % (_tick_s, res['lookback_n']))
    _tier_s = '不限（全部）' if res['tiers'] == 0 else '前 %d 档' % res['tiers']
    _relay_s = '、仅复盘日封板（缩小范围，非完整口径）' if res.get('relay_only') else ''
    _guarantee = ('盘中近似：当日状态随 tick 变化' if is_intra
                  else '历史回测样本均在其中' if res.get('relay_only')
                  else '与回测统计口径一致')
    st.info('复盘日 **%s**（下一交易日 **%s**）　|　市场最高连板 **%d 板**　|　'
            '梯队 %s%s、近 %d 日涨停≥%d　|　共 **%d 只**（%s）'
            % (base, nxt_s, res['market_height'], _tier_s, _relay_s,
               res['lookback_n'], res['min_count'], len(cand), _guarantee))

    show = cand.copy()
    if len(show):
        show['first_up_time'] = show['first_up_time'].replace('', '-')
        show['tier_gap'] = show['tier_gap'].apply(
            lambda x: '-' if x is None or pd.isna(x) else int(x))
        show['board_type'] = show['board_type'].map(_WATCH_BOARD_MAP).fillna('未知')
        show = show.rename(columns={
            'code': '代码', 'name': '名称', 'full_code': '完整代码',
            'sector': '板块', 'limit_up_days': '复盘日连板', 'tier_gap': '距最高板',
            'lu_n': '近%d日涨停' % res['lookback_n'], 'board_type': '复盘日板型',
            'first_up_time': '首封时间', 'limit_up_price': '涨停价',
            'open_price': '开盘价'})
        show['板块'] = (show['板块'].astype(str)
                        .str.replace('THY3·', '', regex=False))
    st.dataframe(show, use_container_width=True, hide_index=True,
                 column_config={
                     '涨停价': st.column_config.NumberColumn(format='%.2f'),
                     '开盘价': st.column_config.NumberColumn(format='%.2f')})

    # ---- 盘中盯盘视图：只读叠加 stock_instant 最新快照，不改变观察池/回测口径 ----
    st.markdown('##### 📡 盘中快照叠加')
    if st.checkbox('加载盘中快照', value=False, key='review_lu10_load_instant',
                   help='读取 stock_instant_YYYYMM 中下一交易日的**最新一个 tick**，'
                        '把清单个股的现价/涨跌幅/封板状态叠加到本表。纯展示、只读，'
                        '封板状态按快照现价与涨停价近似判定，非策略信号。'):
        with st.spinner('正在读取盘中快照...'):
            snap_res = lu.load_intraday_snapshot(
                None if nxt is None else pd.Timestamp(nxt))
        if snap_res.get('error'):
            st.warning(snap_res['error'])
        else:
            snap = snap_res['snapshot'].set_index('code').rename(
                columns={'limit_up_price': 'intraday_lu'})
            merged = cand.merge(
                snap[['price', 'chg_pct', 'intraday_lu', 'board_state', 'amount']],
                left_on='code', right_index=True, how='left')
            present = merged[merged['price'].notna()].copy()
            absent = int(merged['price'].isna().sum())
            sd = pd.Timestamp(snap_res['trade_date']).strftime('%Y-%m-%d')
            tt = pd.Timestamp(snap_res['tick_time']).strftime('%H:%M:%S')
            n_seal = int((present['board_state'] == '封板中').sum())
            n_one = int((present['board_state'] == '一字封板').sum())
            n_brk = int((present['board_state'] == '炸板').sum())
            st.info('快照日 **%s**　最新 tick **%s**　|　清单命中快照 **%d/%d**'
                    '（缺 %d）　|　封板中 %d、一字封板 %d、炸板 %d'
                    % (sd, tt, len(present), len(merged), absent, n_seal, n_one, n_brk))
            view = present.copy()
            view['board_type'] = view['board_type'].map(_WATCH_BOARD_MAP).fillna('未知')
            view['amount_yi'] = view['amount'] / 1e8
            view = view.rename(columns={
                'code': '代码', 'name': '名称', 'sector': '板块',
                'limit_up_days': '复盘日连板', 'board_type': '复盘日板型',
                'price': '现价', 'chg_pct': '涨跌幅%', 'intraday_lu': '当日涨停价',
                'board_state': '盘中状态', 'amount_yi': '成交额(亿)'})
            view = view[['代码', '名称', '板块', '复盘日连板', '复盘日板型', '现价',
                         '涨跌幅%', '当日涨停价', '盘中状态', '成交额(亿)']]
            _state_order = {'一字封板': 0, '封板中': 1, '炸板': 2, '未封板': 3}
            view['_so'] = view['盘中状态'].map(_state_order).fillna(9)
            view = view.sort_values(['_so', '涨跌幅%'],
                                    ascending=[True, False]).drop(columns='_so') \
                       .reset_index(drop=True)
            st.dataframe(view, use_container_width=True, hide_index=True,
                         column_config={
                             '现价': st.column_config.NumberColumn(format='%.2f'),
                             '涨跌幅%': st.column_config.NumberColumn(format='%.2f'),
                             '当日涨停价': st.column_config.NumberColumn(format='%.2f'),
                             '成交额(亿)': st.column_config.NumberColumn(format='%.2f')})
            st.caption('盘中状态由快照现价/最高价与当日涨停价近似判定：'
                       '「封板中」现价=涨停价；「一字封板」开盘即涨停且仍封住；'
                       '「炸板」盘中触板后现价打开；其余为未封板。快照为稀疏 tick，'
                       '非逐笔，仅作盘中状态参考，**不构成任何买卖信号、不影响回测口径**。'
                       + ('　有 %d 只候选在该快照中缺失（可能停牌或不在快照范围）。' % absent
                          if absent else ''))

    st.caption('说明：「复盘日连板=0、板型=未封板」的个股，指复盘日未封板、但近 N 日涨停次数达标者，'
               '属完整统计口径的必要组成；早期无真实涨停表的月份，板型由日线炸板标记近似。'
               '本视图仅为已收盘行情的客观统计与呈现，不构成任何投资建议；'
               '后续是否关注及如何操作，由使用者自行判断。'
               + ('　⚠ 当前已勾选「仅显示复盘日收盘封板的个股」，未封板个股已被剔除，'
                  '清单为非完整口径的筛选视图。'
                  if res.get('relay_only') else ''))

    fname = 'lu10_watchlist_%s.csv' % base
    _d1, _d2 = st.columns([1, 1], gap='small')
    with _d1:
        st.download_button('下载复盘清单（UTF-8）', data=to_csv(show),
                           file_name=fname, key='review_lu10_dl_utf8',
                           use_container_width=True)
    with _d2:
        st.download_button('下载复盘清单（GBK）', data=to_csv_gbk(show),
                           file_name=fname.replace('.csv', '_gbk.csv'),
                           key='review_lu10_dl_gbk', use_container_width=True,
                           help='若用 UTF-8 版本打开中文乱码，请下载此版本。')


def fmt(x):
    return '-' if x is None or pd.isna(x) else '%.2f' % x


# 纯数字代码列：Excel/WPS 打开 CSV 会把 000510 识别成数字 510 吃掉前导零。
# UI 下载（UTF-8/GBK 两版）均面向 Excel 打开，统一补零为 6 位并包一层 ="000510"
# 公式文本，Excel/WPS 打开显示完整 6 位代码；平台回导时由 factor_lab 侧剥离包装。
_CODE_COLS = ('code', '代码')


def _csv_fix_codes(df, excel=False):
    out = df.copy()
    for c in out.columns:
        if str(c) not in _CODE_COLS:
            continue
        digits = (out[c].astype(str).str.extract(r'(\d+)', expand=False)
                  .fillna('').str.zfill(6))
        out[c] = [('="%s"' % s) if excel and s else s for s in digits]
    return out


def to_csv(df):
    # UI 下载均面向 Excel/WPS 打开，两个版本都用 ="000510" 公式文本保住前导零
    return _csv_fix_codes(df, excel=True).to_csv(
        index=False, encoding='utf-8-sig')


def to_csv_gbk(df):
    return _csv_fix_codes(df, excel=True).to_csv(
        index=False).encode('gb18030', errors='replace')


# -------------------- Tab 今日复盘·强势板块（强势板块强势股·情景A） --------------------

_LEADER_BOARD_MAP = {'one_word': '一字板', 't_board': 'T字板',
                     'turnover': '换手板'}


def tab_sector_watch(state):
    """今日复盘·强势板块：T 日盘后建盯盘清单，独立连库，与 lu10_ge2（近10日涨停≥2）
    完全隔离，容忍 state=None。板块与龙头排序与 strongest_sector_a 回测同口径。"""
    st.subheader('🐉 强势板块（强势板块强势股 · 情景A）')
    

    s = st.session_state
    default_date = s.get('end').date() if s.get('end') is not None else None

    c1, c2, c3 = st.columns(3)
    base_date = c1.date_input('复盘日期（当日已收盘）', value=default_date,
                              key='review_sector_base_date',
                              help='默认取侧栏结束日（库内最新交易日）。')
    top_sectors = c2.number_input('候选板块数（前 N 强）', min_value=1, max_value=10,
                                  value=3, key='review_sector_top_sectors')
    top_leaders = c3.number_input('每板块高标数（龙头+卡位）', min_value=1, max_value=8,
                                  value=3, key='review_sector_top_leaders')

    st.button('🐉 生成复盘清单', type='primary', key='review_sector_run')
    if s.get('review_sector_run'):
        with st.spinner('正在生成复盘清单...'):
            res = lu.build_sector_leader_watchlist(
                base_date=base_date, top_sectors=int(top_sectors),
                top_leaders=int(top_leaders))
        s.last_sector_watch = res

    res = s.get('last_sector_watch')
    if res is None:
        rx_info('设置参数后点击「生成复盘清单」。')
        return
    if res.get('error'):
        st.warning(res['error'])
        return

    base = pd.Timestamp(res['base_date']).strftime('%Y-%m-%d')
    nxt = res['next_date']
    intraday = res.get('intraday')
    is_intra = bool(intraday and intraday.get('is_intraday'))
    nxt_s = ('待定（盘中）' if is_intra and nxt is None
             else pd.Timestamp(nxt).strftime('%Y-%m-%d') if nxt is not None
             else '待定')
    sectors, cand = res['sectors'], res['candidates']
    if is_intra:
        _tick_s = pd.Timestamp(intraday['tick_time']).strftime('%Y-%m-%d %H:%M:%S')
        st.warning('⚡ **本次为盘中口径：快照 %s**　|　当日涨停事件表尚未生成，'
                   '本清单为**近似版**：仅统计快照中现价封板的个股（另有炸板 %d 只未计入）；'
                   '板块按 THY3 概念归属（非盘后题材口径）；**无首封时间、无开板次数、'
                   '无法识别 T 字板**，板块/龙头在并列时按成交额排序。结果随 tick 变化，'
                   '**收盘后请用盘后口径重新生成**。'
                   % (_tick_s, int(intraday.get('n_broke', 0))))
    st.info('复盘日 **%s**（下一交易日 **%s**）　|　市场最高连板 **%d 板**　|　'
            '候选板块 **%d** 个、每板块前 **%d** 只高标　|　共 **%d** 只'
            % (base, nxt_s, res['market_height'], len(sectors),
               res['top_leaders'], len(cand)))

    # ---- 板块总览 ----
    if len(sectors):
        sv = sectors.copy()
        for _c in ('sector', 'sector_title'):
            if _c in sv.columns:
                sv[_c] = (sv[_c].astype(str)
                          .str.replace('THY3·', '', regex=False))
        sv['first_seal'] = sv['first_seal'].replace('', '-')
        sv['leader_buyable'] = sv['leader_buyable'].map(
            {True: '否', False: '是'})
        sv = sv.rename(columns={
            'rank': '排名', 'sector': '板块', 'sector_title': '板块全称',
            'up_count': '涨停数', 'n_stocks': '涨停个股', 'max_days': '最高连板',
            'first_seal': '最早首封', 'leader_code': '龙头代码',
            'leader_name': '龙头', 'leader_h': '龙头连板',
            'leader_buyable': '龙头一字板'})
        st.markdown('**板块总览**')
        st.dataframe(sv, use_container_width=True, hide_index=True)

    # ---- 龙头/卡位明细 ----
    show = cand.copy()
    if len(show):
        show['first_up_time'] = show['first_up_time'].replace('', '-')
        if is_intra:
            show['first_up_time'] = '盘中无数据'
            show['limit_open_count'] = show['limit_open_count'].apply(
                lambda x: '-' if x is None or pd.isna(x) else x)
        show['board_type'] = show['board_type'].map(_LEADER_BOARD_MAP).fillna('-')
        show['buyable'] = show['buyable'].map({True: '是', False: '否'})
        show = show.rename(columns={
            'sector_rank': '板块排名', 'sector': '板块', 'sector_up_count': '板块涨停数',
            'role': '角色', 'code': '代码', 'name': '名称', 'full_code': '完整代码',
            'limit_up_days': '复盘日连板', 'first_up_time': '首封时间',
            'limit_open_count': '开板次数', 'board_type': '板型',
            't_close': '收盘价', 'next_limit_price': '次日涨停参考',
            'buyable': '非一字板', 'is_scenario_a': '符合情景A',
            'scenario_note': '备注'})
        show = show.drop(columns=['sector_title'])
    st.markdown('**个股明细（龙头 / 卡位）**')
    st.dataframe(show, use_container_width=True, hide_index=True,
                 column_config={
                     '收盘价': st.column_config.NumberColumn(format='%.2f'),
                     '次日涨停参考': st.column_config.NumberColumn(format='%.2f')})

    st.caption('说明：「次日涨停参考」= 复盘日收盘价 × 涨停幅度'
               '（主板10%、创业板/科创板20%，四舍五入到分），实际以券商涨停价为准。'
               '常见观察维度包括：次日竞价阶段龙头高开幅度与封单量、'
               '早盘板块是否继续成片上板、一字板龙头次日是否开板回封或资金转向卡位股等。'
               '本视图仅为盘后数据的客观呈现，不构成任何投资建议；'
               '后续是否关注及如何操作，由使用者自行判断。')

    fname = 'sector_leader_watch_%s.csv' % base
    _d1, _d2 = st.columns([1, 1], gap='small')
    with _d1:
        st.download_button('下载复盘清单（UTF-8）', data=to_csv(show),
                           file_name=fname, key='review_sector_dl_utf8',
                           use_container_width=True)
    with _d2:
        st.download_button('下载复盘清单（GBK）', data=to_csv_gbk(show),
                           file_name=fname.replace('.csv', '_gbk.csv'),
                           key='review_sector_dl_gbk', use_container_width=True,
                           help='若用 UTF-8 版本打开中文乱码，请下载此版本。')


# -------------------- Tab 今日复盘（近10日涨停≥2 / 强势板块） --------------------

def tab_review(state):
    """今日复盘：承载两个盘后功能视图，二者均独立连库、容忍 state=None。"""
    t_lu10, t_sector = st.tabs(['近10日涨停≥2', '强势板块'])
    with t_lu10:
        tab_watch_pool(state)
    with t_sector:
        tab_sector_watch(state)


# -------------------- Tab2 自选池管理 --------------------

def _read_import_table(up):
    """读取导入文件，兼容真正的 .xls 以及同花顺常见的“伪 xls”。

    同花顺部分导出文件后缀为 .xls，实际是 GBK 编码的制表符分隔文本
    （文件开头常见“代码\\t”），Excel 能打开但 xlrd 无法识别。
    故：先按真 xls 解析，失败再按纯文本读，并自动嗅探分隔符、逐编码回退；
    CSV 同样可能是 GBK 编码，一并兼容。
    """
    raw = up.read()
    import io
    if up.name.lower().endswith('.xls'):
        try:
            return pd.read_excel(io.BytesIO(raw), dtype=str,
                                 engine='xlrd').fillna('')
        except Exception:
            pass
    last_err = None
    for enc in ('utf-8-sig', 'gbk', 'gb18030'):
        try:
            return pd.read_csv(io.BytesIO(raw), dtype=str, sep=None,
                               engine='python', encoding=enc).fillna('')
        except Exception as e:
            last_err = e
    raise last_err


def tab_watchlists(state):
    st.markdown('<span class="rxlib-watch-marker" style="display:none"></span>',
                unsafe_allow_html=True)
    st.subheader('自选池管理')
    st.caption('在此新建、导入、修改你自己的股票池；保存在 config/watchlists 目录，不写入数据库。')
    names = ls.list_watchlists()
    col1, col2 = st.columns([1, 2])
    with col1:
        name = st.text_input('新建 / 选择股票池名称', value='', placeholder='例如：我的观察池')
        if st.button('载入该池') and name:
            st.session_state.wl_name = name
        if names:
            st.caption('已有股票池：')
            for n in names:
                if st.button(n, key='wlbtn_%s' % n):
                    st.session_state.wl_name = n
    cur_name = st.session_state.get('wl_name') or name
    if not cur_name:
        rx_info('输入名称并点击“载入该池”，或在左侧点击已有股票池。')
        return
    st.markdown('**当前编辑：%s**' % cur_name)
    df = ls.load_watchlist(cur_name)

    up = st.file_uploader('导入文件（CSV / Excel .xls，含代码列，可带 SH/SZ 前缀）',
                          type=['csv', 'xls'])
    if up is not None:
        try:
            imp = _read_import_table(up)
            # 代码列：优先 rxcode / code / 代码，否则取第一列（同花顺导出表头即“代码”）
            code_col = next((c for c in imp.columns
                             if str(c).strip().lower() in ('rxcode', 'code', '代码')),
                            imp.columns[0])
            rxcodes = [fl.normalize_rxcode(x) for x in imp[code_col]]
            name_col = next((c for c in imp.columns
                             if str(c).strip() in ('name', '名称', '名字')), None)
            names = list(imp[name_col]) if name_col is not None else [''] * len(imp)
            rows = [dict(rxcode=rx, name=nm)
                    for rx, nm in zip(rxcodes, names) if rx]
            imp_df = pd.DataFrame(rows, columns=['rxcode', 'name'])
            df = pd.concat([df, imp_df],
                           ignore_index=True).drop_duplicates('rxcode')
            st.success('已导入 %d 条，记得点下方保存。' % len(imp_df))
        except Exception as e:
            st.error('导入失败：%s；请确认是 CSV 或 Excel 表格（.xlsx 请另存为 .xls 或 CSV）。' % e)

    pasted = st.text_area('批量粘贴代码（逗号/空格/换行分隔，可带 SH/SZ 前缀）', height=80)
    if st.button('追加粘贴的代码') and pasted:
        import re
        toks = re.split(r'[,\s;，；]+', pasted)
        rxcodes = [c for c in (fl.normalize_rxcode(x) for x in toks) if c]
        add = pd.DataFrame([dict(rxcode=rx, name='') for rx in rxcodes],
                           columns=['rxcode', 'name'])
        df = pd.concat([df, add], ignore_index=True).drop_duplicates('rxcode')
        st.success('追加 %d 条，记得点保存。' % len(add))

    st.markdown('**编辑名单（可直接增删改）**')
    edit_df = df[['rxcode', 'name']].copy()
    edit_df.insert(0, '序号', range(1, len(edit_df) + 1))
    edited = st.data_editor(edit_df, num_rows='dynamic',
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                '序号': st.column_config.NumberColumn(
                                    '序号', width='small', disabled=True),
                                'rxcode': st.column_config.TextColumn(
                                    'rxcode', width='medium'),
                                'name': st.column_config.TextColumn('名称', width='medium')})
    c = st.columns(4)
    if c[0].button('💾 保存', type='primary'):
        n = ls.save_watchlist(cur_name, edited)
        st.success('已保存 %d 只到股票池「%s」。' % (n, cur_name))
    if c[1].button('🗑 删除该股票池'):
        ls.delete_watchlist(cur_name)
        st.session_state.pop('wl_name', None)
        st.success('已删除，刷新页面生效。')
    c[2].download_button('下载该池 CSV（UTF-8）',
                         data=to_csv(edited[['rxcode', 'name']]),
                         file_name='%s.csv' % cur_name)
    c[2].download_button('下载该池（Excel 中文兼容 GBK）',
                         data=to_csv_gbk(edited[['rxcode', 'name']]),
                         file_name='%s_gbk.csv' % cur_name,
                         help='若用 UTF-8 版本打开中文乱码，请下载此版本。')


# -------------------- Tab3 系统设置 --------------------

def tab_settings(state):
    st.subheader('系统设置')
    settings = ls.load_settings()

    st.markdown('**直连数据库参数**')
    db = ls.get_db_config()
    c1, c2 = st.columns(2)
    host = c1.text_input('主机地址 host', value=db['host'])
    port = c2.number_input('端口 port', min_value=1, max_value=65535,
                           value=int(db.get('port', 3306)), step=1)
    database = c1.text_input('数据库 database', value=db['database'])
    user = c2.text_input('用户名 user', value=db['user'])
    password = c1.text_input('密码 password', value=db['password'], type='password')
    cc = st.columns(3)
    if cc[0].button('🔌 测试连接'):
        trial = dict(host=host, port=int(port), database=database,
                     user=user, password=password)
        try:
            n = ls.test_connection(trial)
            st.success('连接成功！stock_base 共 %d 条记录。' % n)
        except Exception as e:
            st.error('连接失败：%s' % e)
    if cc[1].button('💾 保存连接参数'):
        platform_env.save_db_config(dict(host=host, port=int(port),
                                         database=database, user=user,
                                         password=password))
        st.success('连接参数已保存到 config/config.ini，刷新页面后生效。')

    st.divider()
    st.markdown('**API 数据服务参数**')
    st.caption('当左侧"数据提供层"选择 API 时使用；base_url 为服务版本根地址，令牌以 Bearer 方式携带。')
    api = platform_env.get_api_config()
    api_base = st.text_input('API 地址 base_url', value=api['base_url'],
                             placeholder='https://host/api/v1')
    api_token = st.text_input('访问令牌 token', value=api['token'],
                              type='password')
    ac1, ac2 = st.columns(2)
    api_timeout = ac1.number_input('超时（秒）', min_value=1, max_value=300,
                                   value=int(api.get('timeout', 10)), step=1)
    api_verify = st.checkbox('校验 HTTPS 证书', value=bool(api.get('verify_ssl', True)))
    apc = st.columns(3)
    if apc[0].button('🔌 测试 API'):
        trial = dict(base_url=api_base, token=api_token, timeout=int(api_timeout),
                     verify_ssl=api_verify)
        try:
            import data_provider
            prov = data_provider.ApiProvider(**trial)
            try:
                meta = prov.meta()
                latest = meta.get('latest_trade_date') or prov.latest_trade_date()
                exp = str(meta.get('key_expires_at') or '').strip()
                if exp:
                    exp_dt = pd.to_datetime(exp)
                    days = int((exp_dt.normalize() -
                                pd.Timestamp.now().normalize()).days)
                    tip = 'Token 有效期至 %s（剩余 %d 天）' % (
                        exp_dt.strftime('%Y-%m-%d'), days)
                else:
                    tip = 'Token 长期有效（未设到期时间）'
                st.success('连接成功！最新交易日：%s；%s' % (latest, tip))
            finally:
                prov.close()
        except Exception as e:
            st.error('连接失败：%s' % e)
    if apc[1].button('💾 保存 API 参数'):
        platform_env.save_api_config(dict(base_url=api_base, token=api_token,
                                          timeout=int(api_timeout),
                                          verify_ssl=api_verify))
        get_panels.clear()
        st.success('API 参数已保存到 config/config.ini，面板将使用新参数重新加载。')

    st.divider()
    st.markdown('**登录密码**')
    st.caption('需验证当前密码；保存后只落 PBKDF2 哈希，本地不保存明文。改密成功后旧登录令牌立即失效。')
    op = st.text_input('当前密码', type='password', value='', key='set_pwd_old')
    np1 = st.text_input('新密码', type='password', value='', key='set_pwd1')
    np2 = st.text_input('确认新密码', type='password', value='', key='set_pwd2')
    st.caption('新密码长度 %d~%d 位，首尾不能含空格。'
               % (ls.MIN_PASSWORD_LEN, ls.MAX_PASSWORD_LEN))
    if st.button('修改密码'):
        strength_err = ls.validate_password(np1)
        if not op:
            st.warning('请输入当前密码。')
        elif not np1:
            st.warning('新密码不能为空。')
        elif strength_err:
            st.error(strength_err)
        elif np1 != np2:
            st.error('两次输入的新密码不一致。')
        elif np1 == op:
            st.warning('新密码不能与当前密码相同。')
        else:
            try:
                ls.change_password(op, np1)
            except ValueError as e:
                st.error(str(e))
            else:
                ls.rotate_cookie_secret()
                get_panels.clear()
                for k in ('set_pwd_old', 'set_pwd1', 'set_pwd2'):
                    st.session_state.pop(k, None)
                components.html(
                    _set_cookie_js(ls.COOKIE_NAME, ls.make_auth_token(),
                                   ls.COOKIE_DAYS), height=0)
                st.success('密码已更新。')

    st.divider()
    st.markdown('**面板缓存版本**')
    st.caption('改版本号会让旧缓存判定失效并自动重建。代码结构版本前进时本值会'
               '自动前向同步，通常无需手工调整；手动调大可强制重建。')
    cur_ver = ls.get_panel_version()
    new_ver = st.number_input('版本号', min_value=1, value=cur_ver, step=1)
    if st.button('保存版本号'):
        settings = ls.load_settings()
        settings['panel_version'] = int(new_ver)
        ls.save_settings(settings)
        get_panels.clear()
        st.success('版本号已设为 %d，将重新加载/构建面板。' % new_ver)
    st.divider()
    st.markdown('**面板磁盘持久化**')
    st.caption('开启后，组装好的面板会保存到本地，重启平台无需重新计算（占用一定磁盘空间）；关闭则只保留内存缓存，重启后重建。')
    cache_cfg = platform_env.get_cache_config()
    disk_on = st.checkbox('面板保存到本地磁盘',
                          value=bool(cache_cfg.get('panel_disk_cache', False)))
    if st.button('保存磁盘缓存设置'):
        platform_env.save_cache_config({'panel_disk_cache': disk_on})
        get_panels.clear()
        st.success('已%s面板磁盘持久化，面板将按新设置重新加载。'
                   % ('开启' if disk_on else '关闭'))

    st.divider()
    st.markdown('**平台标题（左上角）**')
    title = st.text_input('平台标题', value=settings['title'])
    if st.button('保存标题'):
        settings['title'] = title or ls.DEFAULT_TITLE
        ls.save_settings(settings)
        st.success('已保存，刷新页面后左上角更新。')


# -------------------- Tab4 操作说明 --------------------

def tab_help(state):
    st.markdown('<span class="rxlib-help-marker" style="display:none"></span>',
                unsafe_allow_html=True)
    st.subheader('操作说明')
    st.caption('本页是平台完整使用手册；各控件旁的 ❔ 图标也有即时提示。')
    st.markdown(HELP_TEXT, unsafe_allow_html=False)


HELP_TEXT = """
## 一、平台是做什么的

本平台专用于**涨停打板**策略研究：在历史数据上验证打板回测，并可查看某一交易日的打板选股名单（选**单日区间**即当天选股）。
所有计算都走唯一内核 `limit_up_lab`（复用 `factor_lab` 数据面板），界面只负责收集参数、调用内核、展示结果，
**全程对数据库只读，不写任何业务数据表**。

**基本使用流程**：输入访问密码登录 → 左侧侧边栏设置数据源与打板参数 → 进入「历史回测」→ 点击「运行」。

## 二、登录、退出与修改密码

- **登录**：打开平台需输入访问密码。登录成功后会在浏览器保存一个 30 天有效的登录令牌，
  期间再次打开无需重复输入。连续输错 5 次会临时锁定 5 分钟。
- **退出登录**：左侧侧边栏底部有「退出登录」按钮，点击后清理本机登录态并回到登录页。
  退出会同时清空本次会话的参数、运行历史等临时数据。
- **修改密码**：在「系统设置 → 登录密码」中输入当前密码和新密码。
  新密码长度 4~64 位、首尾不能含空格，且需两次输入一致。
- **密码保存在哪里**：密码**只以 PBKDF2 哈希**（加随机盐、迭代 20 万次）保存在本机
  `config/_factorlab_settings.json`，系统不保存明文，也无法从哈希反推密码。

## 三、标签页说明

| 标签页 | 作用 |
|---|---|
| **历史回测** | 用当前打板参数在所选区间完整回测，给出收益、风险指标、净值/回撤曲线、交易明细（含未卖出持仓的浮盈） |
| **今日复盘** | 盘后盘面统计视图，内含两个功能：「近10日涨停≥2」（统计所选交易日满足条件的个股，口径与 lu10_ge2 回测严格一致）、「强势板块」（按强势板块强势股·情景A 口径对所选交易日的板块与龙头排序） |
| **自选池管理** | 新建/导入/编辑你自己的股票池（本地保存） |
| **系统设置** | 配置数据源连接/API 参数、修改登录密码、面板缓存版本、面板磁盘持久化、平台标题 |
| **操作说明** | 本手册 |

## 四、数据源（API 模式 / MySQL 直连）

平台支持两种取数方式，在「系统设置」中配置：

- **API 模式（推荐）**：通过一个 API Key 从 rxlib_api 服务取数。
  - Key 自身的权限决定可用功能；**盘中数据权限由 Key 自动识别**，无需额外开关。
- **MySQL 直连模式**：直接连接本机/局域网行情库，支持收盘日线与盘中快照
  （盘中读 `stock_instant_YYYYMM`，需库内已生成当日快照）。

配置信息（服务地址、Key 或数据库账号）保存在本机 `config/config.ini`，升级版本不会被覆盖。
数据库连接用完即关，平台对其只读。

## 五、股票池 / 区间参数

- **股票池**：可选「全A」「按板块（沪市主板/深市主板/创业板/科创板/北交所）」
  以及你在「自选池管理」里建立的自定义池。
- **排除 ST 股**：勾选后剔除风险警示股票。
- **排除次新股**：勾选后剔除上市不满「天数门槛」（默认 90 天）的股票。
- **开始/结束日期**：回测区间。系统会自动向开始日期之前多取约 2 个月数据用于指标预热，
  因此连板/量比/位置等指标在窗口开头也是有效的。**开始=结束（单日区间）即当天打板选股**，见下节。
- **并行进程数**：首次构建面板时使用的 CPU 进程数，核多可调大；Windows 下若出现卡住，可设为 1。

## 六、打板参数详解

### 6.1 买入模式

| 模式 | 含义 |
|---|---|
| **低位首板（低吸）** | 近 20 日内首次涨停 且 收盘价 ≤ 前 20 日最高 × `高位阈值(low_hi_mult)` |
| **首板（当日涨停）** | 当日收盘涨停 且 近 20 日内无涨停 |
| **高封板强度首板** | 首板 且 收盘几乎贴着涨停价（封死强板） |
| **连板高度≥N** | 当日连板数 ≥ N（`连板高度下限`） |
| **连板接力** | 昨日连板 且 当日继续涨停（`接力昨日连板数下限`） |

### 6.2 买入时点

- **当日涨停收盘**：涨停日按涨停价成交（打板，可能因一字板买不进被跳过）。
- **次日开盘追板**：信号日次日开盘价成交（一字涨停买不进则跳过）。
- **次日收盘追板**：信号日次日收盘价成交。

### 6.3 连板 / 封板条件

- **连板高度下限（N）**：`连板高度≥N` 模式的阈值。
- **接力昨日连板数下限**：`连板接力` 模式的阈值。
- **近 N 日有涨停（0=关闭）**：近 N 个交易日内出现过涨停才保留。
- **高位阈值（低吸用）**：`低位首板` 判定低位的成本位分位阈值。
- **封板强度量比阈值**：`高封板强度首板` 的量比上限参考。

### 6.4 出场条件

| 参数 | 含义 |
|---|---|
| **止损** | 相对买入价最大亏损，触发当日按理论价成交；空=关闭 |
| **止盈** | 相对买入价盈利目标，触发当日按理论价成交；空=关闭 |
| **最大持有交易日** | 持有达该交易日后到期收盘卖出；0=不限 |
| **断板即出（plan_sell）** | 次日未继续封板即按收盘价离场；打板稳健离场核心开关 |
| **炸板即出（breakout_sell）** | 盘中冲高回落不再封板即离场；可与断板同开 |

### 6.5 板块共振 + 板块涨幅

- **板块涨停家数下限**：所属板块当日涨停家数 ≥ 该值才可打板。
- **板块涨停家数上限**：过滤过热板块；与下限相同或 0 表示不限。
- **涨停口径**：`seal` 只看收盘封板；`touch` 含当日炸板（盘中触板，打板更贴合）。
- **板块涨幅下限**：所属板块当日等权涨幅 ≥ 该值才保留；0 表示禁用。
- **板块层级**：按 TGN 概念 / THY1 一级 / THY2 二级 / THY3 三级 聚合，默认 THY3。
- 板块数据由 DB 构建；DB 不可用时该组条件自动失效。

## 七、费率

- **单边费率（bp）**：买入、卖出各收一次，1 bp = 0.01%。例如万分之 3 填 3；默认 0 表示不计费。

## 八、自选池怎么用

1. 进入「自选池管理」，输入名称（如「我的观察池」），点「载入该池」。
2. 可通过三种方式添加：上传 CSV / Excel 文件（`.csv`、`.xlsx`、`.xls`，
   含 `code` 列）、在文本框批量粘贴、或在表格里直接增删改。
3. 点「保存」后，该池会出现在左侧股票池下拉框（前缀「自定义 ·」）。

代码统一为 6 位数字（如 `600000`、`000001`），无需交易所后缀；
也兼容同花顺等导出的写法：可带 `SH`/`SZ`/`BJ` 前缀（如 `SZ300014`、`SH600066`），
不足 6 位的数字（如 Excel 里 `003931` 显示成 `3931`）会自动补零。
最简单的模板只需一列，首行表头 `code`，每行一个代码。

## 九、本地缓存、面板版本与发布裁剪

平台本地有**两层缓存**，理解它们的区别有助于正确使用「面板缓存版本」：

1. **日线原始数据缓存（月片）**：按月分片保存在
   `data/cache/_factorlab_kline_YYYYMM.pkl`，两种数据源共用，读过的月份离线也可复用。
   它存的是"还没算过指标"的原始行情。
2. **面板缓存（算好指标的结果）**：把月片行情按股票池组装、并计算完连板、量比、位置、
   共振等全部指标后的结果。面板缓存又分两处：**内存缓存**（重启即清）
   和可选的**磁盘缓存**（见下）。

### 9.1 面板缓存版本（系统设置 → 面板缓存版本）

- **是什么**：一个整数版本号，保存在本机 `config/_factorlab_settings.json`，与代码结构版本配套。
- **作用**：它是面板缓存"身份证"的一部分。系统给每一份面板做缓存标记时，
  会把**版本号和股票池、板块、是否排除 ST/次新、自定义代码**等条件一起算进缓存键。
  当你**递增版本号**时，旧缓存判定为不匹配、自动重建。
- **什么时候才需要改**：只在**指标算法或面板结构发生变化**时递增。日常更换股票池、改日期、
  普通开关过滤条件都**不需要**动它。

### 9.2 面板磁盘持久化（系统设置 → 面板磁盘持久化）

- 开启后，组装并计算好的面板会额外保存为本地 `data/cache/_factorlab_panel_*.pkl`，
  **重启平台后无需重新计算**；关闭时只保留内存缓存。
- 磁盘面板同样带结构版本校验，结构升级后旧面板自动失效并重建。

### 9.3 发布裁剪

- 若希望发布给他人时只携带最近 3 或 6 个月的数据以减小体积，在项目根目录运行：

  ```powershell
  \\.prune-cache.ps1 -KeepMonths 6      # 先预览
  \\.prune-cache.ps1 -KeepMonths 6 -Apply   # 确认后真正删除
  ```

  裁剪以最新月片为基准，不影响数据源中原始数据；之后需要更长区间时可重新取数。

## 十、结果与可溯源

- 每次运行都提供「参数快照」（运行时间、股票池、区间、全部参数、数据源、缓存标签），可下载 JSON。
- 交易明细可下载 CSV（统一 `utf-8-sig` 编码，Excel 可直接打开）。
- 屏幕显示与下载文件中的代码统一为 6 位数字（`code` 列），全平台一致。
"""


# -------------------- 会话运行历史 --------------------

def show_history():
    hist = st.session_state.get('history', [])
    if not hist:
        return
    with st.sidebar.expander('会话运行历史（%d）' % len(hist)):
        for h in reversed(hist):
            st.caption('%s | %s | %s~%s' % (h['run_time'], h['source'],
                                            h['start'][5:], h['end'][5:]))


# -------------------- 主流程 --------------------

def main():
    global_chrome_styles()
    mark_run_buttons()
    brand_header()
    login_gate()
    sidebar_controls()
    state = panels_or_message()
    show_history()
    tabs = st.tabs(['历史回测', '今日复盘', '自选池管理', '系统设置', '操作说明'])
    funcs = [tab_limit, tab_review, tab_watchlists, tab_settings, tab_help]
    for tab, fn in zip(tabs, funcs):
        with tab:
            if state is None and fn not in (tab_review, tab_settings, tab_help):
                rx_info('面板数据暂不可用，请在「系统设置」中检查数据源或更换 API Key。')
                continue
            fn(state)


if __name__ == '__main__':
    main()
