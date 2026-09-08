import { useEffect, useState } from 'react';

async function request(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error('The request could not be completed. Please try again.');
  return response.status === 204 ? null : response.json();
}

export default function App() {
  const [customers, setCustomers] = useState([]);
  const [meta, setMeta] = useState(null);
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    let active = true;
    Promise.all([request('/api/customers'), request('/api/meta')])
      .then(([records, environment]) => {
        if (active) { setCustomers(records); setMeta(environment); }
      })
      .catch(() => { if (active) setError('Cannot reach this environment. Start it, then reload this page.'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  async function addCustomer(event) {
    event.preventDefault();
    setBusy(true); setError(''); setNotice('');
    try {
      const customer = await request('/api/customers', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim(), email: email.trim() }),
      });
      setCustomers((records) => [...records, customer]);
      setName(''); setEmail(''); setNotice('Customer added.');
    } catch (failure) { setError(failure.message); }
    finally { setBusy(false); }
  }

  async function removeCustomer(customer) {
    setBusy(true); setError(''); setNotice('');
    try {
      await request(`/api/customers/${customer.id}`, { method: 'DELETE' });
      setCustomers((records) => records.filter((record) => record.id !== customer.id));
      setNotice('Customer removed.');
    } catch (failure) { setError(failure.message); }
    finally { setBusy(false); }
  }

  return (
    <main>
      <header className="masthead">
        <span className="brand">DELIVERY LAB</span>
        <span className="environment">{meta?.environment ?? 'CONNECTING'}</span>
      </header>
      <div className="heading">
        <div><h1>Customer register</h1><p>A small workspace for fictional customer records.</p></div>
        {meta && <p className="release">Release <code>{meta.release.slice(0, 12)}</code></p>}
      </div>
      {error && <p className="message error" role="alert">{error}</p>}
      <p className="notice" role="status">{notice}</p>
      <div className="workspace">
        <section className="panel" aria-labelledby="add-heading">
          <h2 id="add-heading">Add a customer</h2>
          <form onSubmit={addCustomer}>
            <label htmlFor="name">Name</label>
            <input id="name" required maxLength={80} value={name} onChange={(e) => setName(e.target.value)} placeholder="Alex Example" />
            <label htmlFor="email">Email</label>
            <input id="email" type="email" required maxLength={254} value={email} onChange={(e) => setEmail(e.target.value)} placeholder="alex@example.com" />
            <button className="primary" disabled={busy || loading || !meta || !name.trim() || !email.trim()} type="submit">{busy ? 'Saving…' : 'Add customer'}</button>
          </form>
          <p className="hint">Use fictional information only.</p>
        </section>
        <section className="records" aria-labelledby="records-heading" aria-busy={loading}>
          <div className="section-heading"><h2 id="records-heading">Customers</h2><span>{customers.length} records</span></div>
          {loading ? <p>Loading records…</p> : customers.length === 0 ? <div className="empty"><h3>No customers yet</h3><p>Add a fictional customer to test this environment.</p></div> :
            <ul className="customer-list">{customers.map((customer) => <li key={customer.id}>
              <div><strong>{customer.name}</strong><span>{customer.email}</span></div>
              <button className="remove" disabled={busy} aria-label={`Remove ${customer.name}`} onClick={() => removeCustomer(customer)}>Remove</button>
            </li>)}</ul>}
        </section>
      </div>
      <footer>Local case study demo · Each environment stores its own records.</footer>
    </main>
  );
}
