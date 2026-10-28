import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { motion } from 'framer-motion';
import { MousePointer2, RefreshCw, Zap, Trash2, CheckCircle2, Plus, Loader2 } from 'lucide-react';
import { showToast } from '../components/common/ToastContainer';

interface RelayCard {
    index: number;
    email: string;
    cardType: string;
    cardCode: string;
    active: boolean;
    renewCount: number;
    lastRenewAt: number | null;
}

interface RelayHistory {
    at: number;
    ok: boolean;
    card: string;
    message: string;
    deviceId?: string;
    reason?: 'manual' | 'auto' | string;
}

interface RelayState {
    state?: { status?: string; message?: string };
    activeToken?: string;
    deviceId?: string;
    cards: RelayCard[];
    history?: RelayHistory[];
}

// 生产环境由 nginx 把 /cursor/ 反代到 relay(8317)；本地开发可用 VITE_CURSOR_RELAY_BASE 覆盖
const API_BASE = `${import.meta.env.VITE_CURSOR_RELAY_BASE || '/cursor'}/admin/api`;

async function relayApi<T>(path: string, body?: unknown): Promise<T> {
    const key = sessionStorage.getItem('abv_admin_api_key') || '';
    const res = await fetch(API_BASE + path, {
        method: body === undefined ? 'GET' : 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${key}` },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) {
        let msg = String(res.status);
        try {
            msg = (await res.json()).detail || msg;
        } catch {
            /* 非 JSON 错误体，保留状态码 */
        }
        throw new Error(msg);
    }
    return res.json();
}

const statusColor = (s?: string) =>
    s === 'success' ? 'text-green-600 dark:text-green-400'
        : s === 'error' ? 'text-red-500' : 'text-amber-500';

const ago = (sec?: number | null) => {
    if (!sec) return '';
    const d = Math.floor(Date.now() / 1000 - sec);
    if (d < 60) return '刚刚';
    if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
    if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
    return `${Math.floor(d / 86400)} 天前`;
};

const fmtTime = (sec: number) => new Date(sec * 1000).toLocaleString();

const CursorRelay: React.FC = () => {
    const { t } = useTranslation();
    const [data, setData] = useState<RelayState | null>(null);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);
    const [email, setEmail] = useState('');
    const [cardCode, setCardCode] = useState('');
    const busyRef = useRef(false);

    const load = useCallback(async () => {
        try {
            setData(await relayApi<RelayState>('/state'));
            setError('');
        } catch (e) {
            setError((e as Error).message);
        }
    }, []);

    useEffect(() => {
        load();
        const timer = setInterval(() => {
            if (!busyRef.current) load();
        }, 5000);
        return () => clearInterval(timer);
    }, [load]);

    const run = async (path: string, body?: unknown, okMsg?: string) => {
        if (busyRef.current) return;
        busyRef.current = true;
        setBusy(true);
        try {
            setData(await relayApi<RelayState>(path, body ?? {}));
            setError('');
            if (okMsg) showToast(okMsg, 'success');
        } catch (e) {
            showToast((e as Error).message, 'error');
        } finally {
            busyRef.current = false;
            setBusy(false);
        }
    };

    const addCard = async () => {
        const code = cardCode.trim();
        if (!code) return;
        await run('/cards', { cardCode: code, email: email.trim() }, t('cursor_relay.added', '卡密已添加'));
        setCardCode('');
        setEmail('');
    };

    const resetDevice = () => {
        if (!window.confirm(t('cursor_relay.reset_device_confirm', '将生成全新的随机设备 ID 并备份旧值，之后的续杯使用新指纹。是否确认重置？'))) return;
        run('/device/reset', {}, t('cursor_relay.reset_device_done', '设备 ID 已重置'));
    };

    const status = data?.state?.status;
    const cards = data?.cards ?? [];
    const history = data?.history ?? [];

    return (
        <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.2, ease: 'easeOut' }}
            className="h-full overflow-y-auto px-3 sm:px-5 py-4 max-w-4xl mx-auto w-full"
        >
            <div className="flex items-center justify-between gap-3 mb-3">
                <h1 className="text-xl sm:text-2xl font-bold text-gray-900 dark:text-white flex items-center gap-2">
                    <MousePointer2 className="text-blue-500" size={22} />
                    {t('cursor_relay.title', 'Cursor 中转')}
                </h1>
                <button
                    onClick={load}
                    className="btn btn-sm btn-ghost gap-2 text-gray-600 dark:text-gray-400 min-h-11"
                >
                    <RefreshCw size={16} />
                    {t('cursor_relay.refresh', '刷新')}
                </button>
            </div>

            {error && (
                <div className="mb-3 rounded-xl border border-red-200 dark:border-red-900/40 bg-red-50 dark:bg-red-900/10 px-4 py-3 text-sm text-red-600 dark:text-red-400 break-all">
                    {t('cursor_relay.read_failed', '读取失败')}: {error}
                </div>
            )}

            <div className="bg-white dark:bg-base-100 rounded-xl shadow-sm border border-gray-100 dark:border-base-200 divide-y divide-gray-100 dark:divide-base-200">
                {[
                    [t('cursor_relay.status', '状态'), status ? <span className={statusColor(status)}>{status}</span> : '—'],
                    [t('cursor_relay.detail', '详情'), data?.state?.message || '—'],
                    [t('cursor_relay.active_token', '当前凭证'), data?.activeToken || t('cursor_relay.not_set', '未设置')],
                    [t('cursor_relay.device_id', '设备 ID'), data?.deviceId || '—'],
                ].map(([k, v], i) => (
                    <div key={i} className="flex items-start justify-between gap-3 px-4 py-3">
                        <span className="text-sm text-gray-500 dark:text-gray-400 shrink-0">{k as string}</span>
                        <span className="text-sm font-mono text-right break-all text-gray-900 dark:text-gray-100">{v as React.ReactNode}</span>
                    </div>
                ))}
            </div>

            <button
                onClick={() => run('/renew', {}, t('cursor_relay.renewed', '续杯已触发'))}
                disabled={busy}
                className="w-full mt-3 min-h-12 rounded-xl bg-blue-600 text-white font-bold flex items-center justify-center gap-2 disabled:opacity-50"
            >
                {busy ? <Loader2 size={18} className="animate-spin" /> : <Zap size={18} />}
                {t('cursor_relay.renew_now', '立即续杯')}
            </button>

            <button
                onClick={resetDevice}
                disabled={busy}
                className="w-full mt-2 min-h-11 rounded-xl border border-gray-200 dark:border-base-200 text-sm font-medium text-gray-700 dark:text-gray-300 flex items-center justify-center gap-2 disabled:opacity-50"
            >
                <RefreshCw size={16} />
                {t('cursor_relay.reset_device', '重置设备 ID')}
            </button>

            <div className="mt-4">
                <div className="font-semibold text-sm text-gray-700 dark:text-gray-300 mb-2">
                    {t('cursor_relay.pool', '卡池')} <span className="text-gray-400 font-normal">({cards.length})</span>
                </div>
                <div className="bg-white dark:bg-base-100 rounded-xl shadow-sm border border-gray-100 dark:border-base-200 divide-y divide-gray-100 dark:divide-base-200">
                    {cards.length === 0 && (
                        <p className="px-4 py-6 text-sm text-center text-gray-400">
                            {t('cursor_relay.empty', '暂无卡密')}
                        </p>
                    )}
                    {cards.map((c) => (
                        <div key={c.index} className="px-4 py-3">
                            <div className="flex items-center justify-between gap-2 min-h-11">
                                <span className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                                    {c.email || t('cursor_relay.unnamed', '未命名')}
                                    {c.active && (
                                        <span className="ml-2 text-[11px] px-2 py-0.5 rounded-full bg-green-50 text-green-600 dark:bg-green-900/20 dark:text-green-400">
                                            {t('cursor_relay.current', '当前')}
                                        </span>
                                    )}
                                </span>
                                <span className="text-[11px] px-2 py-0.5 rounded-full bg-gray-100 text-gray-500 dark:bg-base-200 dark:text-gray-400 shrink-0">
                                    {c.cardType}
                                </span>
                            </div>
                            <div className="text-xs text-gray-500 dark:text-gray-400 font-mono break-all mt-1">
                                {c.cardCode} · {t('cursor_relay.renew_count', '续杯')} {c.renewCount} {t('cursor_relay.times', '次')}
                                {c.lastRenewAt ? ` · ${ago(c.lastRenewAt)}` : ''}
                            </div>
                            <div className="flex gap-2 mt-2">
                                <button
                                    onClick={() => run('/active', { index: c.index }, t('cursor_relay.switched', '已切换'))}
                                    disabled={busy || c.active}
                                    className="flex-1 min-h-11 rounded-lg border border-gray-200 dark:border-base-200 text-sm flex items-center justify-center gap-1.5 disabled:opacity-40 text-gray-700 dark:text-gray-300"
                                >
                                    <CheckCircle2 size={15} />
                                    {t('cursor_relay.set_active', '设为当前')}
                                </button>
                                <button
                                    onClick={() => run('/cards/delete', { index: c.index }, t('cursor_relay.deleted', '已删除'))}
                                    disabled={busy}
                                    className="flex-1 min-h-11 rounded-lg border border-red-200 dark:border-red-900/40 text-sm text-red-500 flex items-center justify-center gap-1.5 disabled:opacity-40"
                                >
                                    <Trash2 size={15} />
                                    {t('cursor_relay.delete', '删除')}
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
            </div>

            <div className="mt-4">
                <div className="font-semibold text-sm text-gray-700 dark:text-gray-300 mb-2">
                    {t('cursor_relay.history', '续杯记录')} <span className="text-gray-400 font-normal">({history.length})</span>
                </div>
                <div className="bg-white dark:bg-base-100 rounded-xl shadow-sm border border-gray-100 dark:border-base-200 divide-y divide-gray-100 dark:divide-base-200">
                    {history.length === 0 && (
                        <p className="px-4 py-6 text-sm text-center text-gray-400">
                            {t('cursor_relay.no_history', '暂无续杯记录')}
                        </p>
                    )}
                    {history.map((h, i) => (
                        <div key={`${h.at}-${i}`} className="px-4 py-3">
                            <div className="flex items-center justify-between gap-2">
                                <span className={`text-sm ${h.ok ? 'text-green-600 dark:text-green-400' : 'text-red-500'}`}>
                                    {h.ok ? t('cursor_relay.history_ok', '成功') : t('cursor_relay.history_fail', '失败')}
                                    {h.card ? ` · ${h.card}` : ''}
                                    {h.reason && (
                                        <span className="ml-2 text-[11px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 dark:bg-base-200 dark:text-gray-400">
                                            {h.reason === 'manual'
                                                ? t('cursor_relay.reason_manual', '手动')
                                                : t('cursor_relay.reason_auto', '自动')}
                                        </span>
                                    )}
                                </span>
                                <span className="text-[11px] text-gray-400 shrink-0">{fmtTime(h.at)}</span>
                            </div>
                            {h.message && (
                                <div className="text-xs text-gray-500 dark:text-gray-400 break-all mt-1">{h.message}</div>
                            )}
                            {h.deviceId && (
                                <div className="text-[11px] text-gray-400 font-mono break-all mt-1">{h.deviceId}</div>
                            )}
                        </div>
                    ))}
                </div>
            </div>

            <div className="mt-4 mb-6 bg-white dark:bg-base-100 rounded-xl shadow-sm border border-gray-100 dark:border-base-200 p-4">
                <div className="font-semibold text-sm text-gray-700 dark:text-gray-300 mb-3">
                    {t('cursor_relay.add_card', '添加卡密')}
                </div>
                <input
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder={t('cursor_relay.email_placeholder', '邮箱（备注用，可空）')}
                    autoComplete="off"
                    className="w-full min-h-11 px-3 mb-2 rounded-lg bg-gray-50 dark:bg-base-200 border border-gray-200 dark:border-base-200 text-sm text-gray-900 dark:text-white outline-none focus:border-blue-500"
                />
                <input
                    value={cardCode}
                    onChange={(e) => setCardCode(e.target.value)}
                    placeholder={t('cursor_relay.code_placeholder', '卡密')}
                    autoComplete="off"
                    className="w-full min-h-11 px-3 mb-2 rounded-lg bg-gray-50 dark:bg-base-200 border border-gray-200 dark:border-base-200 text-sm text-gray-900 dark:text-white outline-none focus:border-blue-500"
                />
                <button
                    onClick={addCard}
                    disabled={busy || !cardCode.trim()}
                    className="w-full min-h-11 rounded-lg bg-gray-900 dark:bg-white text-white dark:text-gray-900 text-sm font-semibold flex items-center justify-center gap-1.5 disabled:opacity-40"
                >
                    <Plus size={16} />
                    {t('cursor_relay.add', '添加')}
                </button>
            </div>
        </motion.div>
    );
};

export default CursorRelay;
