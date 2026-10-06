const originalFinanceLoad = load;
load = async () => {
  await originalFinanceLoad();
  const table = document.querySelector('#imports');
  table.replaceChildren(...(state.imports || []).map(item => {
    const row = document.createElement('tr');
    const label = document.createElement('td');
    label.textContent = `${item.filename} · ${item.row_count} Zeilen · ${item.status}`;
    row.append(label);
    const action = document.createElement('td');
    const button = document.createElement('button');
    button.className = 'danger';
    button.textContent = 'Rollback';
    button.disabled = item.status === 'rolled_back';
    button.onclick = () => post('/api/finance/import/rollback', {import_id: item.id}).then(load);
    action.append(button);
    row.append(action);
    return row;
  }));
};
if(!document.querySelector('#app').classList.contains('hidden'))load();
