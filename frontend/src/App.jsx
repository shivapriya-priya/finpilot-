import { useEffect, useState } from 'react'

const API = import.meta.env.VITE_API_URL || 'http://127.0.0.1:8000'

export default function App() {
  const [status, setStatus] = useState('checking...')
  useEffect(() => {
    fetch(`${API}/health`)
      .then(r => r.json())
      .then(d => setStatus(d.status))
      .catch(() => setStatus('cannot reach backend'))
  }, [])
  return (
    <div className="min-h-screen bg-slate-100 flex items-center justify-center">
      <div className="bg-white rounded-2xl shadow p-8 text-center">
        <h1 className="text-3xl font-bold text-slate-900">FinPilot</h1>
        <p className="mt-2 text-slate-500">
          Backend status: <b className="text-emerald-600">{status}</b>
        </p>
      </div>
    </div>
  )
}