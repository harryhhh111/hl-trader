export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (!path.startsWith("/api/v1/")) {
    throw new Error("API paths must use the same-origin /api/v1/ prefix");
  }
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...init, credentials: "same-origin", headers });
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ApiError("服务返回了无效响应", response.status);
  }
  if (!response.ok) {
    const detail = payload && typeof payload === "object" && "detail" in payload
      ? (payload as { detail?: unknown }).detail
      : undefined;
    throw new ApiError(typeof detail === "string" ? detail : "请求失败", response.status);
  }
  if (!payload || typeof payload !== "object" || !("data" in payload)) {
    throw new ApiError("服务响应缺少 data", response.status);
  }
  return (payload as { data: T }).data;
}
