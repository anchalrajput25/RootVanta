"use client";

// The whole dashboard is one page: the login form, or the incident screen once logged in.

import { useEffect, useState } from "react";
import Dashboard from "@/components/Dashboard";
import Login from "@/components/Login";
import { api, LOGGED_OUT_EVENT, savedToken, saveToken, takeSignInResult } from "@/lib/api";
import type { Me } from "@/lib/types";

export default function Page() {
  const [me, setMe] = useState<Me | null>(null);
  const [checking, setChecking] = useState(true);
  const [message, setMessage] = useState("");

  // Back from company sign-in with a token (or a reason it failed), or a token saved earlier in this tab.
  useEffect(() => {
    const back = takeSignInResult();
    if (back.token) saveToken(back.token);
    if (back.error) setMessage(back.error);
    if (!savedToken()) {
      setChecking(false);
      return;
    }
    api.me().then(setMe).catch(() => saveToken(null)).finally(() => setChecking(false));
  }, []);

  useEffect(() => {
    const onLoggedOut = () => {
      setMe(null);
      setMessage("Your session ended. Please log in again.");
    };
    window.addEventListener(LOGGED_OUT_EVENT, onLoggedOut);
    return () => window.removeEventListener(LOGGED_OUT_EVENT, onLoggedOut);
  }, []);

  async function start(token: string) {
    saveToken(token);
    setMe(await api.me());
    setMessage("");
  }

  function logout() {
    saveToken(null);
    setMe(null);
  }

  if (checking) return null;
  if (!me) return <Login message={message} onToken={start} />;
  return <Dashboard me={me} onLogout={logout} />;
}
