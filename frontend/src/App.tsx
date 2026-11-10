import { useEffect, useState } from 'react'
import { apiClient, authSession } from './api/apiClient'
import { UploadView } from './views/UploadView'
import { RuleManagementView } from './views/RuleManagementView'
import { LoginView } from './views/LoginView'
import { UserManagementView } from './views/UserManagementView'
import { LanguageSwitch } from './components/LanguageSwitch'
import { useI18n } from './i18n'

type Page = 'document' | 'rules' | 'users'

/** Re-renders whenever the user signs in or out. */
function useSignedIn(): boolean {
  const [signedIn, setSignedIn] = useState(authSession.isSignedIn)
  useEffect(() => authSession.subscribe(() => setSignedIn(authSession.isSignedIn)), [])
  return signedIn
}

function App() {
  const [page, setPage] = useState<Page>('document')
  const { t } = useI18n()
  const signedIn = useSignedIn()
  const user = signedIn ? authSession.user : null
  const isAdmin = user?.isAdmin ?? false
  // Regular users only process documents; rules and users are admin pages.
  const current: Page = isAdmin ? page : 'document'

  return (
    <div className="app-layout">
      <header className="app-header">
        <span className="app-header-title">{t('app.title')}</span>
        {isAdmin && (
          <ul className="app-nav">
            {(['document', 'rules', 'users'] as const).map((p) => (
              <li key={p}>
                <button className={current === p ? 'active' : ''} onClick={() => setPage(p)}>
                  {t(`nav.${p}`)}
                </button>
              </li>
            ))}
          </ul>
        )}
        <LanguageSwitch />
        {signedIn && (
          <span className="app-user">
            <span>{user?.email}</span>
            <button onClick={() => authSession.signOut()}>{t('auth.signOut')}</button>
          </span>
        )}
      </header>
      <main className="app-content">
        {!signedIn ? (
          <div className="app-card app-card-login">
            <LoginView session={authSession} />
          </div>
        ) : (
          <div
            className={
              current === 'document'
                ? 'app-card app-card-narrow'
                : current === 'users'
                  ? 'app-card app-card-users'
                  : 'app-card'
            }
          >
            {current === 'document' && <UploadView apiClient={apiClient} />}
            {current === 'rules' && <RuleManagementView apiClient={apiClient} />}
            {current === 'users' && <UserManagementView apiClient={apiClient} />}
          </div>
        )}
      </main>
    </div>
  )
}

export default App
