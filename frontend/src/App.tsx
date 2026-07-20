import { useState } from 'react'
import './App.css'
import { ChatPage } from './pages/ChatPage'
import { DocumentsPage } from './pages/DocumentsPage'
import { EmailsPage } from './pages/EmailsPage'
import { LeadsPage } from './pages/LeadsPage'

type Tab = 'leads' | 'documents' | 'emails' | 'chat'

const TABS: { key: Tab; label: string }[] = [
  { key: 'leads', label: 'Leads' },
  { key: 'documents', label: 'Strategy Docs' },
  { key: 'emails', label: 'Email Approval' },
  { key: 'chat', label: 'Chatbot' },
]

function App() {
  const [tab, setTab] = useState<Tab>('leads')

  return (
    <div className="app">
      <header>
        <h1>AI Marketing Outreach</h1>
        <nav>
          {TABS.map((t) => (
            <button
              key={t.key}
              className={tab === t.key ? 'active' : ''}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>
      <main>
        {tab === 'leads' && <LeadsPage />}
        {tab === 'documents' && <DocumentsPage />}
        {tab === 'emails' && <EmailsPage />}
        {tab === 'chat' && <ChatPage />}
      </main>
    </div>
  )
}

export default App
