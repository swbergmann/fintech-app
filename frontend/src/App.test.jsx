import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, test, vi } from 'vitest';
import App from './App.jsx';

test('adds a customer and removes it after a successful server response', async () => {
  const customer = { id: 1, name: 'Alex Example', email: 'alex@example.com' };
  const fetchMock = vi.fn(async (path, options) => {
    const data = path === '/api/meta' ? { environment: 'DEV', release: 'abc123' }
      : options?.method === 'POST' ? customer : [];
    return { ok: true, status: options?.method === 'DELETE' ? 204 : 200, json: async () => data };
  });
  vi.stubGlobal('fetch', fetchMock);
  const user = userEvent.setup();
  render(<App />);
  expect(await screen.findByText('DEV')).toBeInTheDocument();
  await user.type(screen.getByLabelText('Name'), customer.name);
  await user.type(screen.getByLabelText('Email'), customer.email);
  await user.click(screen.getByRole('button', { name: 'Add customer' }));
  expect(await screen.findByText(customer.name)).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith('/api/customers', expect.objectContaining({
    method: 'POST', body: JSON.stringify({ name: customer.name, email: customer.email }),
  }));
  await user.click(screen.getByRole('button', { name: 'Remove Alex Example' }));
  await waitFor(() => expect(screen.queryByText(customer.name)).not.toBeInTheDocument());
});

test('reports an unavailable backend and prevents submission', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Offline')));
  render(<App />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Cannot reach this environment');
  expect(screen.getByRole('button', { name: 'Add customer' })).toBeEnabled(); // Intentional failure for the CI demonstration.
});

test('keeps a record visible when deletion fails', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path, options) => ({
    ok: options?.method !== 'DELETE', status: 200,
    json: async () => path === '/api/meta' ? { environment: 'UAT', release: 'abc' }
      : [{ id: 1, name: 'Alex Example', email: 'alex@example.com' }],
  })));
  render(<App />);
  await userEvent.click(await screen.findByRole('button', { name: 'Remove Alex Example' }));
  expect(await screen.findByRole('alert')).toBeInTheDocument();
  expect(screen.getByText('Alex Example')).toBeInTheDocument();
});
