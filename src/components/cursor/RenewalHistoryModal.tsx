import React, { useCallback, useEffect, useState, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import {
    X,
    RefreshCw,
    Search,
    CheckCircle2,
    XCircle,
    Copy,
    Check,
    History,
    Trash2,
    Cpu,
    Zap,
    Clock,
    RotateCcw,
    AlertCircle,
} from 'lucide-react';
import Pagination from '../common/Pagination';
import { copyToClipboard } from '../../utils/clipboard';
import { showToast } from '../common/ToastContainer';
import {
    RelayHistory,
    fetchRenewalHistory,
    clearRenewalHistory,
} from '../../services/cursorRelayService';

interface RenewalHistoryModalProps {
    isOpen: boolean;
    onClose: () => void;
}

const fmtFullTime = (sec?: number) => {
    if (!sec) return '—';
    const d = new Date(sec * 1000);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
};

const ago = (sec?: number | null) => {
    if (!sec) return '';
    const d = Math.floor(Date.now() / 1000 - sec);
    if (d < 5) return '刚刚';
    if (d < 60) return `${d} 秒前`;
    if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
    if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
    return `${Math.floor(d / 86400)} 天前`;
};

export const RenewalHistoryModal: React.FC<RenewalHistoryModalProps> = ({
    isOpen,
    onClose,
}) => {
    const { t } = useTranslation();
    const [loading, setLoading] = useState(false);
    const [clearing, setClearing] = useState(false);
    const [items, setItems] = useState<RelayHistory[]>([]);
    const [total, setTotal] = useState(0);
    const [currentPage, setCurrentPage] = useState(1);
    const [pageSize, setPageSize] = useState(20);
    const [statusFilter, setStatusFilter] = useState<'all' | 'success' | 'failed'>('all');
    const [reasonFilter, setReasonFilter] = useState<'all' | 'manual' | 'auto'>('all');
    const [searchKeyword, setSearchKeyword] = useState('');
    const [copiedKey, setCopiedKey] = useState<string | null>(null);

    const loadHistory = useCallback(async () => {
        setLoading(true);
        try {
            const data = await fetchRenewalHistory({
                page: currentPage,
                pageSize,
                status: statusFilter,
                reason: reasonFilter,
                keyword: searchKeyword,
            });
            setItems(data.items || []);
            setTotal(data.total || 0);
        } catch (err) {
            showToast((err as Error).message || t('cursor_relay.history_modal.load_failed', '加载续杯历史失败'), 'error');
        } finally {
            setLoading(false);
        }
    }, [currentPage, pageSize, statusFilter, reasonFilter, searchKeyword, t]);

    useEffect(() => {
        if (isOpen) {
            loadHistory();
        }
    }, [isOpen, loadHistory]);

    // ESC 键关闭模态框
    useEffect(() => {
        const handleKeyDown = (e: KeyboardEvent) => {
            if (e.key === 'Escape' && isOpen) {
                onClose();
            }
        };
        window.addEventListener('keydown', handleKeyDown);
        return () => window.removeEventListener('keydown', handleKeyDown);
    }, [isOpen, onClose]);

    const handleCopy = async (text: string, key: string) => {
        const ok = await copyToClipboard(text);
        if (ok) {
            setCopiedKey(key);
            showToast(t('common.copied', '已复制到剪贴板'), 'success');
            setTimeout(() => setCopiedKey(null), 2000);
        } else {
            showToast(t('common.copy_failed', '复制失败'), 'error');
        }
    };

    const handleClear = async () => {
        if (!window.confirm(t('cursor_relay.history_modal.confirm_clear', '确认清空所有历史续杯记录？清空后无法恢复。'))) {
            return;
        }
        setClearing(true);
        try {
            await clearRenewalHistory();
            showToast(t('cursor_relay.history_modal.cleared', '续杯记录已清空'), 'success');
            setCurrentPage(1);
            loadHistory();
        } catch (err) {
            showToast((err as Error).message || t('cursor_relay.history_modal.clear_failed', '清空失败'), 'error');
        } finally {
            setClearing(false);
        }
    };

    const totalPages = useMemo(() => Math.ceil(total / Math.max(1, pageSize)), [total, pageSize]);

    if (!isOpen) return null;

    return createPortal(
        <div
            className="fixed inset-0 z-[120] flex items-center justify-center bg-black/60 backdrop-blur-sm p-2 sm:p-4"
            onClick={onClose}
        >
            <div
                className="bg-white dark:bg-base-100 rounded-2xl shadow-2xl w-full max-w-4xl h-[90vh] max-h-[820px] flex flex-col overflow-hidden border border-gray-200 dark:border-base-200 animate-in fade-in zoom-in-95 duration-200"
                onClick={(e) => e.stopPropagation()}
            >
                {/* Header */}
                <div className="px-5 py-4 border-b border-gray-100 dark:border-base-200 bg-gray-50/70 dark:bg-base-200/50 flex items-center justify-between shrink-0">
                    <div className="flex items-center gap-3">
                        <div className="w-9 h-9 rounded-xl bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-400 flex items-center justify-center shadow-xs">
                            <History size={20} />
                        </div>
                        <div>
                            <div className="flex items-center gap-2">
                                <h3 className="font-bold text-base sm:text-lg text-gray-900 dark:text-base-content">
                                    {t('cursor_relay.history_modal.title', 'Cursor 续杯历史记录')}
                                </h3>
                                <span className="text-xs px-2.5 py-0.5 rounded-full bg-blue-100/80 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 font-mono font-semibold">
                                    {total}
                                </span>
                            </div>
                            <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">
                                {t('cursor_relay.history_modal.subtitle', '查看卡密自动轮换、扣时长充能与换号记录，免密码直接浏览')}
                            </p>
                        </div>
                    </div>

                    <div className="flex items-center gap-1.5">
                        <button
                            onClick={loadHistory}
                            disabled={loading}
                            className="btn btn-sm btn-ghost btn-circle text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-base-content"
                            title={t('common.refresh', '刷新')}
                        >
                            <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
                        </button>
                        {total > 0 && (
                            <button
                                onClick={handleClear}
                                disabled={clearing || loading}
                                className="btn btn-sm btn-ghost btn-circle text-red-500 hover:bg-red-50 dark:hover:bg-red-950/30"
                                title={t('cursor_relay.history_modal.clear', '清空历史记录')}
                            >
                                <Trash2 size={16} />
                            </button>
                        )}
                        <button
                            onClick={onClose}
                            className="btn btn-sm btn-ghost btn-circle text-gray-400 hover:text-gray-700 dark:hover:text-base-content ml-1"
                            title={t('common.close', '关闭')}
                        >
                            <X size={18} />
                        </button>
                    </div>
                </div>

                {/* Filters */}
                <div className="px-5 py-3 border-b border-gray-100 dark:border-base-200 bg-white dark:bg-base-100 flex flex-wrap items-center justify-between gap-3 shrink-0">
                    <div className="flex flex-wrap items-center gap-2">
                        {/* 状态筛选 */}
                        <div className="inline-flex rounded-lg bg-gray-100 dark:bg-base-200 p-0.5 text-xs font-medium">
                            <button
                                onClick={() => {
                                    setStatusFilter('all');
                                    setCurrentPage(1);
                                }}
                                className={`px-2.5 py-1.5 rounded-md transition-all ${
                                    statusFilter === 'all'
                                        ? 'bg-white dark:bg-base-100 text-gray-900 dark:text-white shadow-xs font-bold'
                                        : 'text-gray-500 hover:text-gray-900 dark:hover:text-white'
                                }`}
                            >
                                {t('common.all', '全部')}
                            </button>
                            <button
                                onClick={() => {
                                    setStatusFilter('success');
                                    setCurrentPage(1);
                                }}
                                className={`px-2.5 py-1.5 rounded-md transition-all flex items-center gap-1 ${
                                    statusFilter === 'success'
                                        ? 'bg-white dark:bg-base-100 text-green-600 dark:text-green-400 shadow-xs font-bold'
                                        : 'text-gray-500 hover:text-green-600 dark:hover:text-green-400'
                                }`}
                            >
                                <span className="w-1.5 h-1.5 rounded-full bg-green-500"></span>
                                {t('cursor_relay.history_ok', '成功')}
                            </button>
                            <button
                                onClick={() => {
                                    setStatusFilter('failed');
                                    setCurrentPage(1);
                                }}
                                className={`px-2.5 py-1.5 rounded-md transition-all flex items-center gap-1 ${
                                    statusFilter === 'failed'
                                        ? 'bg-white dark:bg-base-100 text-red-500 shadow-xs font-bold'
                                        : 'text-gray-500 hover:text-red-500'
                                }`}
                            >
                                <span className="w-1.5 h-1.5 rounded-full bg-red-500"></span>
                                {t('cursor_relay.history_fail', '失败')}
                            </button>
                        </div>

                        {/* 来源筛选 */}
                        <div className="inline-flex rounded-lg bg-gray-100 dark:bg-base-200 p-0.5 text-xs font-medium">
                            <button
                                onClick={() => {
                                    setReasonFilter('all');
                                    setCurrentPage(1);
                                }}
                                className={`px-2.5 py-1.5 rounded-md transition-all ${
                                    reasonFilter === 'all'
                                        ? 'bg-white dark:bg-base-100 text-gray-900 dark:text-white shadow-xs font-bold'
                                        : 'text-gray-500 hover:text-gray-900 dark:hover:text-white'
                                }`}
                            >
                                {t('cursor_relay.history_modal.all_reasons', '全部来源')}
                            </button>
                            <button
                                onClick={() => {
                                    setReasonFilter('manual');
                                    setCurrentPage(1);
                                }}
                                className={`px-2.5 py-1.5 rounded-md transition-all ${
                                    reasonFilter === 'manual'
                                        ? 'bg-white dark:bg-base-100 text-blue-600 dark:text-blue-400 shadow-xs font-bold'
                                        : 'text-gray-500 hover:text-blue-600'
                                }`}
                            >
                                {t('cursor_relay.reason_manual', '手动')}
                            </button>
                            <button
                                onClick={() => {
                                    setReasonFilter('auto');
                                    setCurrentPage(1);
                                }}
                                className={`px-2.5 py-1.5 rounded-md transition-all ${
                                    reasonFilter === 'auto'
                                        ? 'bg-white dark:bg-base-100 text-purple-600 dark:text-purple-400 shadow-xs font-bold'
                                        : 'text-gray-500 hover:text-purple-600'
                                }`}
                            >
                                {t('cursor_relay.reason_auto', '自动')}
                            </button>
                        </div>
                    </div>

                    {/* 搜索框 */}
                    <div className="relative flex-1 min-w-[200px] max-w-xs">
                        <Search
                            size={14}
                            className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
                        />
                        <input
                            type="text"
                            value={searchKeyword}
                            onChange={(e) => {
                                setSearchKeyword(e.target.value);
                                setCurrentPage(1);
                            }}
                            placeholder={t('cursor_relay.history_modal.search_hint', '搜索卡密 / 错误 / 设备 ID...')}
                            className="w-full text-xs rounded-lg pl-8 pr-3 py-1.5 bg-gray-50 dark:bg-base-200 border border-gray-200 dark:border-base-300 focus:outline-none focus:border-blue-500 text-gray-900 dark:text-base-content"
                        />
                    </div>
                </div>

                {/* Content List */}
                <div className="flex-1 overflow-y-auto p-4 sm:p-5 space-y-3 bg-gray-50/50 dark:bg-base-200/30">
                    {loading && items.length === 0 ? (
                        <div className="h-64 flex flex-col items-center justify-center text-gray-400">
                            <RefreshCw size={24} className="animate-spin text-blue-500 mb-2" />
                            <span className="text-xs">{t('common.loading', '加载中...')}</span>
                        </div>
                    ) : items.length === 0 ? (
                        <div className="h-64 flex flex-col items-center justify-center text-gray-400">
                            <AlertCircle size={32} className="opacity-40 mb-2" />
                            <span className="text-sm font-medium">
                                {searchKeyword || statusFilter !== 'all' || reasonFilter !== 'all'
                                    ? t('cursor_relay.history_modal.no_matches', '未找到匹配的续杯记录')
                                    : t('cursor_relay.no_history', '暂无续杯记录')}
                            </span>
                            <span className="text-xs text-gray-400 mt-1">
                                {t('cursor_relay.history_modal.empty_tip', '当触发续杯或额度自动重置时，记录会自动留痕于此处')}
                            </span>
                        </div>
                    ) : (
                        items.map((h, idx) => {
                            const isSuccess = Boolean(h.ok);
                            const isReset = h.action === 'reset';
                            const cardKey = `item-${h.at}-${idx}`;

                            return (
                                <div
                                    key={cardKey}
                                    className="bg-white dark:bg-base-100 rounded-xl border border-gray-100 dark:border-base-200 p-4 shadow-2xs hover:border-gray-300 dark:hover:border-base-300 transition-all duration-150"
                                >
                                    <div className="flex items-start justify-between gap-3">
                                        <div className="flex items-center gap-2.5 flex-wrap">
                                            {/* Status Badge */}
                                            <div
                                                className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-semibold ${
                                                    isSuccess
                                                        ? 'bg-green-50 text-green-700 dark:bg-green-950/30 dark:text-green-400 border border-green-200/70 dark:border-green-900/40'
                                                        : 'bg-red-50 text-red-600 dark:bg-red-950/30 dark:text-red-400 border border-red-200/70 dark:border-red-900/40'
                                                }`}
                                            >
                                                {isSuccess ? (
                                                    <CheckCircle2 size={14} className="text-green-600 dark:text-green-400" />
                                                ) : (
                                                    <XCircle size={14} className="text-red-500" />
                                                )}
                                                <span>
                                                    {isSuccess
                                                        ? isReset
                                                            ? t('cursor_relay.history_modal.reset_quota', '扣时长重置额度 (+60次)')
                                                            : t('cursor_relay.history_modal.switch_card', '极速换号')
                                                        : t('cursor_relay.history_fail', '失败')}
                                                </span>
                                            </div>

                                            {/* Reason Badge */}
                                            {h.reason && (
                                                <span
                                                    className={`inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded font-medium ${
                                                        h.reason === 'manual'
                                                            ? 'bg-blue-50 text-blue-600 dark:bg-blue-900/20 dark:text-blue-300'
                                                            : 'bg-purple-50 text-purple-600 dark:bg-purple-900/20 dark:text-purple-300'
                                                    }`}
                                                >
                                                    {h.reason === 'manual' ? (
                                                        <Zap size={11} />
                                                    ) : (
                                                        <RotateCcw size={11} />
                                                    )}
                                                    {h.reason === 'manual'
                                                        ? t('cursor_relay.reason_manual', '手动')
                                                        : t('cursor_relay.reason_auto', '自动')}
                                                </span>
                                            )}

                                            {/* Card detail */}
                                            {h.card && (
                                                <span className="font-mono text-xs font-semibold text-gray-800 dark:text-gray-200 bg-gray-100 dark:bg-base-200 px-2 py-0.5 rounded">
                                                    {h.card}
                                                </span>
                                            )}
                                        </div>

                                        {/* Timestamp */}
                                        <div className="flex items-center gap-1.5 text-xs text-gray-400 dark:text-gray-500 shrink-0 font-mono">
                                            <Clock size={12} className="opacity-70" />
                                            <span title={fmtFullTime(h.at)}>{fmtFullTime(h.at)}</span>
                                            <span className="text-[11px] text-gray-400 hidden sm:inline">
                                                ({ago(h.at)})
                                            </span>
                                        </div>
                                    </div>

                                    {/* Error or message details */}
                                    {h.message && (
                                        <div className="mt-2.5 p-2.5 rounded-lg bg-gray-50 dark:bg-base-200/60 border border-gray-100 dark:border-base-200 text-xs text-gray-700 dark:text-gray-300 break-all flex items-start justify-between gap-2 group">
                                            <span className="font-mono leading-relaxed select-text">
                                                {h.message}
                                            </span>
                                            <button
                                                onClick={() => handleCopy(h.message!, `msg-${idx}`)}
                                                className="btn btn-ghost btn-xs text-gray-400 hover:text-gray-700 dark:hover:text-white shrink-0 p-1"
                                                title={t('common.copy', '复制')}
                                            >
                                                {copiedKey === `msg-${idx}` ? (
                                                    <Check size={13} className="text-green-500" />
                                                ) : (
                                                    <Copy size={13} />
                                                )}
                                            </button>
                                        </div>
                                    )}

                                    {/* Device ID */}
                                    {h.deviceId && (
                                        <div className="mt-2 flex items-center justify-between text-[11px] text-gray-400 dark:text-gray-500 font-mono">
                                            <span className="flex items-center gap-1 truncate max-w-[85%]" title={h.deviceId}>
                                                <Cpu size={12} className="shrink-0 opacity-60" />
                                                <span className="truncate">{h.deviceId}</span>
                                            </span>
                                            <button
                                                onClick={() => handleCopy(h.deviceId!, `dev-${idx}`)}
                                                className="hover:text-blue-500 transition-colors flex items-center gap-1 shrink-0 ml-2"
                                                title={t('cursor_relay.history_modal.copy_device_id', '复制设备 ID')}
                                            >
                                                {copiedKey === `dev-${idx}` ? (
                                                    <span className="text-green-500 font-sans text-[10px]">{t('common.copied', '已复制')}</span>
                                                ) : (
                                                    <Copy size={11} />
                                                )}
                                            </button>
                                        </div>
                                    )}
                                </div>
                            );
                        })
                    )}
                </div>

                {/* Footer with Pagination */}
                <div className="border-t border-gray-100 dark:border-base-200 bg-white dark:bg-base-100 px-4 py-2 shrink-0">
                    <Pagination
                        currentPage={currentPage}
                        totalPages={totalPages}
                        onPageChange={(p) => setCurrentPage(p)}
                        totalItems={total}
                        itemsPerPage={pageSize}
                        onPageSizeChange={(newSize) => {
                            setPageSize(newSize);
                            setCurrentPage(1);
                        }}
                        pageSizeOptions={[10, 20, 50, 100]}
                    />
                </div>
            </div>
        </div>,
        document.body
    );
};

export default RenewalHistoryModal;
