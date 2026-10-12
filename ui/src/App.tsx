import { useCallback, useEffect, useState } from "react";
import { Agents } from "./Agents";
import { api, ApiError, errText, type Config, type Me } from "./api";
import { DocumentPage } from "./DocumentPage";
import { DomainPage } from "./DomainPage";
import { Home } from "./Home";
import { NewDocument } from "./NewDocument";
import { Proposals } from "./Proposals";
import { Templates } from "./Templates";
import { Link, navigate, useLocation } from "./router";

function signinError(): string | null {
  const e = new URLSearchParams(location.search).get("signin_error");
  if (e) history.replaceState(null, "", "/");
  return e ? "Sign-in didn't complete. Try again." : null;
}

function SignIn({ config, onDone }: { config: Config; onDone: () => void }) {
  const [name, setName] = useState("dev");
  const [admin, setAdmin] = useState(false);
  if (config.oidc) return <a className="button primary" href="/api/auth/oidc/login">Sign in</a>;
  if (!config.dev_login) return null;
  return (
    <form className="row" onSubmit={(e) => { e.preventDefault(); void api.devLogin(name, admin).then(onDone); }}>
      <input value={name} onChange={(e) => setName(e.target.value)} aria-label="Dev user" size={8} />
      <label className="check"><input type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} />admin</label>
      <button type="submit">Dev sign-in</button>
    </form>
  );
}

function Routes({ me, onCount }: { me: Me | null; onCount: (n: number) => void }) {
  const { path, search } = useLocation();
  let m: RegExpMatchArray | null;
  if ((m = path.match(/^\/domain\/([a-f0-9]{32})$/))) return <DomainPage key={m[1]} id={m[1]} me={me} />;
  if ((m = path.match(/^\/d\/([a-f0-9]{32})$/))) return <DocumentPage id={m[1]} />;
  if (path === "/new" && me) return <NewDocument search={search} />;
  if (path === "/agents" && me) return <Agents />;
  if (path === "/templates" && me) return <Templates me={me} />;
  if (path === "/proposals" && me) return <Proposals onCount={onCount} />;
  return <Home me={me} />;
}

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(signinError);
  const [pending, setPending] = useState(0);
  const { path } = useLocation();
  useEffect(() => { if (me) api.proposalCount().then(setPending, () => undefined); }, [me, path]);

  const loadMe = useCallback(() => {
    api.me().then(setMe, (e) => (e instanceof ApiError && e.status === 401 ? setMe(null) : setError(errText(e))));
  }, []);
  useEffect(() => {
    api.config().then(setConfig, (e) => setError(errText(e)));
    loadMe();
  }, [loadMe]);

  const logout = async () => {
    await api.logout();
    setMe(null);
    navigate("/");
  };

  if (me === undefined || !config) return <div className="center-msg">{error ?? "Loading…"}</div>;
  return (
    <div className="app">
      <header className="topbar">
        <Link to="/" className="brand"><img src="/favicon.svg" alt="" width={20} height={20} /> webdoc</Link>
        <span className="grow" />
        {me ? (
          <>
            <Link to="/proposals">Proposals{pending > 0 && <span className="badge-count">{pending}</span>}</Link>
            <Link to="/templates">Types</Link>
            <Link to="/agents">Agents</Link>
            <span className="muted">{me.username}{me.is_admin && " (admin)"}</span>
            <button onClick={() => void logout()}>Sign out</button>
          </>
        ) : <SignIn config={config} onDone={loadMe} />}
      </header>
      {error && <div className="error-bar">{error}<button onClick={() => setError(null)}>×</button></div>}
      <main><Routes me={me} onCount={setPending} /></main>
    </div>
  );
}
