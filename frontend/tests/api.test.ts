/**
 * Tests for lib/api.ts — the typed fetch client.
 *
 * fetch is stubbed via vi.stubGlobal; every assertion is grounded in the
 * actual apiFetch/login implementation:
 *   - Bearer token from localStorage("cr_token") injected when present
 *   - 401 off the login page → clearToken() + redirect (redirect itself is
 *     a jsdom no-op, so we assert the observable token clearing)
 *   - non-2xx → ApiError(status, body.detail ?? statusText)
 *   - 204 → undefined; 2xx → parsed JSON
 */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import {
  API_URL,
  ApiError,
  apiFetch,
  clearToken,
  getMe,
  getToken,
  login,
  setToken,
} from "@/lib/api";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  // Tests run from a non-login page by default — the 401 handler branches
  // on window.location.pathname.
  window.history.replaceState(null, "", "/dashboard");
});

afterEach(() => {
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  clearToken();
});

describe("token storage", () => {
  test("setToken/getToken/clearToken round-trip via localStorage", () => {
    expect(getToken()).toBeNull();
    setToken("tok-1");
    expect(getToken()).toBe("tok-1");
    expect(window.localStorage.getItem("cr_token")).toBe("tok-1");
    clearToken();
    expect(getToken()).toBeNull();
  });
});

describe("apiFetch auth header", () => {
  test("injects Authorization: Bearer when a token exists", async () => {
    setToken("tok-abc");
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { ok: true }));

    await apiFetch("/anything");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_URL}/anything`);
    expect(new Headers(init.headers).get("Authorization")).toBe(
      "Bearer tok-abc"
    );
  });

  test("sends no Authorization header without a token", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

    await apiFetch("/anything");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Authorization")).toBeNull();
  });
});

describe("apiFetch error handling", () => {
  test("401 off the login page clears the stored token and throws ApiError", async () => {
    setToken("expired");
    fetchMock.mockResolvedValueOnce(
      jsonResponse(401, { detail: "Could not validate credentials" })
    );

    await expect(apiFetch("/me")).rejects.toMatchObject({
      status: 401,
      message: "Could not validate credentials",
    });
    // clearToken() ran (the redirect to /login is unobservable in jsdom).
    expect(getToken()).toBeNull();
  });

  test("401 on the login page leaves the token alone", async () => {
    window.history.replaceState(null, "", "/login");
    setToken("mid-login");
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "bad" }));

    await expect(apiFetch("/me")).rejects.toBeInstanceOf(ApiError);
    expect(getToken()).toBe("mid-login");
  });

  test("5xx propagates as ApiError with the body's detail", async () => {
    setToken("still-valid");
    fetchMock.mockResolvedValueOnce(jsonResponse(500, { detail: "boom" }));

    const err = await apiFetch("/events").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(500);
    expect((err as ApiError).message).toBe("boom");
    // Non-401 failures never log the user out.
    expect(getToken()).toBe("still-valid");
  });

  test("5xx with a non-JSON body falls back to statusText", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("<html>gateway</html>", {
        status: 502,
        statusText: "Bad Gateway",
      })
    );

    await expect(apiFetch("/events")).rejects.toMatchObject({
      status: 502,
      message: "Bad Gateway",
    });
  });
});

describe("apiFetch responses", () => {
  test("getMe parses the JSON body (happy path)", async () => {
    const user = {
      created_at: "2026-06-01T00:00:00Z",
      email: "admin@radar.local",
      full_name: null,
      id: 1,
      is_active: true,
      role: "admin",
    };
    fetchMock.mockResolvedValueOnce(jsonResponse(200, user));

    await expect(getMe()).resolves.toEqual(user);
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_URL}/me`,
      expect.objectContaining({ headers: expect.any(Headers) })
    );
  });

  test("204 resolves to undefined without parsing a body", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));

    await expect(apiFetch<void>("/companies/sources/1")).resolves.toBe(
      undefined
    );
  });
});

describe("login", () => {
  test("posts form-encoded credentials to /auth/token and returns the access token", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, { access_token: "jwt-xyz", token_type: "bearer" })
    );

    await expect(login("a@b.co", "pw")).resolves.toBe("jwt-xyz");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_URL}/auth/token`);
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Content-Type")).toBe(
      "application/x-www-form-urlencoded"
    );
    const body = new URLSearchParams(String(init.body));
    expect(body.get("username")).toBe("a@b.co");
    expect(body.get("password")).toBe("pw");
  });

  test("failed login throws ApiError with the backend detail", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(401, { detail: "Incorrect email or password" })
    );

    await expect(login("a@b.co", "nope")).rejects.toMatchObject({
      status: 401,
      message: "Incorrect email or password",
    });
  });
});
