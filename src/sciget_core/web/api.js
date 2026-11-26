// SPDX-License-Identifier: BSD-3-Clause
// Thin client for the sciget-core local API (docs/api.md, version 1).

const TOKEN_KEY = 'sciget.token';
let memoryToken = null;

/** Move a `#token=` fragment into sessionStorage and clear it from the address bar. */
export function captureToken() {
  const hash = location.hash.startsWith('#') ? location.hash.slice(1) : '';
  const params = new URLSearchParams(hash);
  const token = params.get('token');
  if (token) {
    memoryToken = token;
    try { sessionStorage.setItem(TOKEN_KEY, token); } catch { /* storage blocked: keep it in memory */ }
    history.replaceState(null, '', location.pathname + location.search);
  }
}

export function getToken() {
  if (memoryToken) return memoryToken;
  try { memoryToken = sessionStorage.getItem(TOKEN_KEY); } catch { memoryToken = null; }
  return memoryToken;
}

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

const NOT_RUNNING =
  "Sciget isn't responding. Check that `sciget serve` is still running in your terminal, then reload this page.";

export async function api(method, path, body) {
  const token = getToken();
  if (!token) {
    throw new ApiError(401, 'unauthorized', 'No access token.');
  }
  const headers = { Authorization: `Bearer ${token}`, Accept: 'application/json' };
  const init = { method, headers, cache: 'no-store', credentials: 'omit' };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(`/api/v1${path}`, init);
  } catch {
    throw new ApiError(0, 'offline', NOT_RUNNING);
  }
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = null; }
  }
  if (!res.ok) {
    const code = (data && data.error) || (res.status === 401 ? 'unauthorized' : 'internal');
    const message = (data && data.message) || `The request failed (HTTP ${res.status}).`;
    throw new ApiError(res.status, code, message);
  }
  return data;
}
