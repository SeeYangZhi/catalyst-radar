"use client";

import { Radar } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function LoginPage() {
  const { user, loading, login } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("admin@radar.local");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!loading && user) {
      router.replace("/dashboard");
    }
  }, [user, loading, router]);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
      router.replace("/dashboard");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex items-center gap-2">
          <span className="flex h-9 w-9 items-center justify-center rounded-full bg-surface-1">
            <Radar className="h-5 w-5 text-accent" />
          </span>
          <div>
            <h1 className="font-semibold text-lg tracking-tight">
              Catalyst Radar
            </h1>
            <p className="text-ink-muted text-xs">Operator sign-in</p>
          </div>
        </div>

        <form
          className="rounded-[15px] border border-border bg-card p-6"
          onSubmit={onSubmit}
        >
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="email">Email</Label>
              <Input
                autoComplete="username"
                id="email"
                onChange={(e) => setEmail(e.target.value)}
                required
                type="text"
                value={email}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password">Password</Label>
              <Input
                autoComplete="current-password"
                id="password"
                onChange={(e) => setPassword(e.target.value)}
                required
                type="password"
                value={password}
              />
            </div>

            {error && (
              <p className="text-destructive text-xs" role="alert">
                {error}
              </p>
            )}

            <Button className="w-full" disabled={submitting} type="submit">
              {submitting ? "Signing in…" : "Sign in"}
            </Button>
          </div>
        </form>

        <p className="mt-4 text-center text-[11px] text-ink-muted">
          Registration is disabled. Single-operator access only.
        </p>
      </div>
    </main>
  );
}
