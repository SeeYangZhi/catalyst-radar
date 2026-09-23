"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import {
  login as apiLogin,
  type CurrentUser,
  clearToken,
  getMe,
  getToken,
  setToken,
} from "@/lib/api";

interface AuthState {
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  user: CurrentUser | null;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);

  const loadUser = useCallback(async () => {
    if (!getToken()) {
      setUser(null);
      setLoading(false);
      return;
    }
    try {
      setUser(await getMe());
    } catch {
      clearToken();
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Bootstrap auth from the stored token + backend /me on mount.
    // This is an external-system sync, the intended use of an effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadUser();
  }, [loadUser]);

  const login = useCallback(async (email: string, password: string) => {
    const token = await apiLogin(email, password);
    setToken(token);
    setLoading(true);
    setUser(await getMe());
    setLoading(false);
  }, []);

  const logout = useCallback(() => {
    clearToken();
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return ctx;
}
