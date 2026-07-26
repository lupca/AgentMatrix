import { showError } from './toast';

const BASE_URL = '/api';
const MAX_ATTEMPTS = 3;
const RETRY_DELAY_MS = 250;

export class ApiError extends Error {
  status: number;
  data: any;

  constructor(status: number, message: string, data?: any) {
    super(message);
    this.status = status;
    this.data = data;
    this.name = 'ApiError';
  }
}

async function request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const url = endpoint.startsWith('http')
    ? endpoint
    : `${BASE_URL}${endpoint.startsWith('/') ? endpoint : `/${endpoint}`}`;

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string>),
  };

  const config: RequestInit = {
    ...options,
    headers,
  };

  let lastError: unknown;

  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
    try {
      const response = await fetch(url, config);

      if (!response.ok) {
        let errorData: any;
        try {
          errorData = await response.json();
        } catch {
          errorData = null;
        }
        const message = errorData?.detail || errorData?.message || response.statusText || 'API Request failed';
        throw new ApiError(response.status, message, errorData);
      }

      if (response.status === 204) {
        return {} as T;
      }

      return await response.json();
    } catch (error) {
      if (config.signal?.aborted || (error as any)?.name === 'AbortError') throw error;

      lastError = error;

      const isTransient = !(error instanceof ApiError) || error.status === 408 || error.status === 429 || error.status >= 500;
      if (!isTransient || attempt === MAX_ATTEMPTS) {
        const message = error instanceof Error ? error.message : 'Unable to complete the request';
        showError(message);
        throw error;
      }

      await new Promise((resolve) => setTimeout(resolve, RETRY_DELAY_MS * 2 ** (attempt - 1)));
    }
  }

  const error = lastError instanceof Error ? lastError : new Error('Unable to complete the request');
  showError(error.message);
  throw error;
}

export const api = {
  get: <T>(endpoint: string, options?: RequestInit) =>
    request<T>(endpoint, { ...options, method: 'GET' }),

  post: <T>(endpoint: string, data?: any, options?: RequestInit) =>
    request<T>(endpoint, {
      ...options,
      method: 'POST',
      body: data ? JSON.stringify(data) : undefined,
    }),

  put: <T>(endpoint: string, data?: any, options?: RequestInit) =>
    request<T>(endpoint, {
      ...options,
      method: 'PUT',
      body: data ? JSON.stringify(data) : undefined,
    }),

  patch: <T>(endpoint: string, data?: any, options?: RequestInit) =>
    request<T>(endpoint, {
      ...options,
      method: 'PATCH',
      body: data ? JSON.stringify(data) : undefined,
    }),

  delete: <T>(endpoint: string, options?: RequestInit) =>
    request<T>(endpoint, { ...options, method: 'DELETE' }),
};
