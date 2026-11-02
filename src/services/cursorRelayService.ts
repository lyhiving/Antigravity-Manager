export interface RelayCard {
    index: number;
    email: string;
    cardType: string;
    cardCode: string;
    active: boolean;
    renewCount: number;
    lastRenewAt: number | null;
}

export interface RelayHistory {
    at: number;
    ok: boolean;
    card?: string;
    message?: string;
    deviceId?: string;
    reason?: 'manual' | 'auto' | string;
    action?: 'reset' | 'switch' | string;
    index?: number;
}

export interface RelayDaily {
    date: string;
    switches: number;
    resets: number;
    total: number;
    remaining: number;
    cooldownRemaining: number;
}

export interface RelayState {
    state?: { status?: string; message?: string; phase?: string };
    activeToken?: string;
    autoRenew?: boolean;
    deviceId?: string;
    cards: RelayCard[];
    daily?: RelayDaily;
    history?: RelayHistory[];
}

export interface HistoryQueryParams {
    page: number;
    pageSize: number;
    status?: 'all' | 'success' | 'failed';
    reason?: 'all' | 'manual' | 'auto';
    keyword?: string;
}

export interface HistoryResponse {
    total: number;
    offset: number;
    limit: number;
    items: RelayHistory[];
}

export const RELAY_BASE = import.meta.env.VITE_CURSOR_RELAY_BASE || '/cursor';
export const API_BASE = `${RELAY_BASE}/admin/api`;

export async function relayApi<T>(path: string, body?: unknown): Promise<T> {
    const key = sessionStorage.getItem('abv_admin_api_key') || '';
    const res = await fetch(API_BASE + path, {
        method: body === undefined ? 'GET' : 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...(key ? { Authorization: `Bearer ${key}` } : {}),
        },
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

export async function fetchRenewalHistory(params: HistoryQueryParams): Promise<HistoryResponse> {
    const offset = Math.max(0, (params.page - 1) * params.pageSize);
    const searchParams = new URLSearchParams();
    searchParams.set('limit', String(params.pageSize));
    searchParams.set('offset', String(offset));
    if (params.status && params.status !== 'all') {
        searchParams.set('status', params.status);
    }
    if (params.reason && params.reason !== 'all') {
        searchParams.set('reason', params.reason);
    }
    if (params.keyword && params.keyword.trim()) {
        searchParams.set('keyword', params.keyword.trim());
    }
    return relayApi<HistoryResponse>(`/history?${searchParams.toString()}`);
}

export async function clearRenewalHistory(): Promise<void> {
    await relayApi('/history/clear', {});
}
