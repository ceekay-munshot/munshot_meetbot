"use client";

import { useEffect } from "react";
import { useAuthStore } from "@/stores/auth-store";

interface AuthProviderProps {
  children: React.ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps) {
  const checkAuth = useAuthStore((state) => state.checkAuth);

  // Auth is bypassed: the dashboard never forces email/magic-link login before
  // joining a meeting. The API proxy falls back to the env VEXA_API_KEY, so
  // requests work without a user session. We still run checkAuth() once so that,
  // if a valid session cookie happens to exist, user info (name/email) populates
  // for nicer UI — but we never gate rendering or redirect to /login on its result.
  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  return <>{children}</>;
}
