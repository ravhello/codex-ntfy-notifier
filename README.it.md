# Durable Codex ntfy notifier

[English](README.md) · [Architettura](docs/architecture.md) · [Privacy e sicurezza](docs/security-and-privacy.md) · [Risoluzione problemi](docs/troubleshooting.md)

Notifiche push ntfy affidabili quando un turno OpenAI Codex termina, anche con più finestre VS Code, WSL e host Remote SSH.

> [!IMPORTANT]
> Questo è un progetto community non ufficiale, non affiliato né approvato da OpenAI o ntfy.

## Perché esiste

Un hook con un singolo `curl` può perdere eventi durante timeout o disconnessioni e non distingue bene sessioni concorrenti, WSL, host SSH e subagent. Questo progetto aggiunge:

- outbox atomica su disco prima di qualsiasi richiesta HTTP;
- un worker per host con retry, backoff e jitter;
- deduplica tramite `thread-id + turn-id` e `sequence_id` ntfy stabile;
- supporto Windows, WSL, Linux e Remote SSH;
- soppressione dei subagent con ricontrollo prima dell’invio;
- isolamento dei record corrotti senza bloccare la coda;
- prompt mai memorizzati e messaggio finale escluso per impostazione predefinita.

La garanzia realistica è **at-least-once durevole**, non exactly-once transazionale.

## Installazione Windows e WSL

```powershell
git clone https://github.com/ravhello/codex-ntfy-notifier.git
cd codex-ntfy-notifier
.\install.ps1 -WslDistro Ubuntu
```

Su una nuova installazione il topic viene chiesto con input nascosto. Per Windows senza WSL:

```powershell
.\install.ps1 -NoWsl
```

Ricaricare le finestre VS Code già aperte dopo l’installazione.

## Installazione Linux

```sh
git clone https://github.com/ravhello/codex-ntfy-notifier.git
cd codex-ntfy-notifier
CODEX_NTFY_TOPIC=$(python3 -c 'import getpass; print(getpass.getpass("Topic ntfy privato: "))')
export CODEX_NTFY_TOPIC
./install-linux.sh
unset CODEX_NTFY_TOPIC
```

## Host Remote SSH

Windows, da PowerShell:

```powershell
.\install-remote-windows.ps1 -HostName my-windows-host
```

Linux, da Linux o WSL:

```sh
./install-remote-linux.sh my-linux-host "$HOME/.codex/ntfy-config.json"
```

Ogni host reale mantiene una propria outbox. Quando possibile, usare un token ntfy publish-only diverso per ciascun host.

## Configurazione e privacy

La configurazione privata è `~/.codex/ntfy-config.json`; vedere [ntfy-config.example.json](ntfy-config.example.json).

Impostazioni principali:

- `include_message: false`: non conserva né invia il messaggio finale;
- `include_thread_title: false`: non usa il titolo della task, che può riassumere il prompt;
- `suppress_subagents: true`: elimina le completion delegate;
- `max_attempts: 0`: retry illimitati per errori transitori;
- `dead_retention_days: 30`: retention delle dead-letter sanitizzate;
- `allow_insecure_auth: false`: vieta credenziali su HTTP non locale.

Con `include_message: true`, il messaggio finale viene redatto e troncato, ma la redazione regex è solo best-effort. I prompt utente non vengono mai salvati. Leggere [Privacy e sicurezza](docs/security-and-privacy.md) prima di abilitare il contenuto o distribuire credenziali su host remoti.

## Diagnostica

Windows:

```powershell
~/.codex/notify-ntfy.ps1 -Doctor
Get-ScheduledTask CodexNtfyWatcher | Select-Object TaskName, State
~/.codex/notify-ntfy.ps1 -Test
```

Linux/WSL:

```sh
python3 ~/.codex/notify-ntfy.py --doctor
systemctl --user status codex-ntfy.service
```

## Test

La suite usa soltanto un server HTTP locale finto e non contatta un topic ntfy reale:

```sh
python3 -m unittest discover -s tests -v
```

## Alternative

Esistono notifier più generici come [ai-agent-notifier](https://github.com/DevinoSolutions/ai-agent-notifier), [code-notify](https://github.com/mylee04/code-notify) e [agent-notify](https://github.com/paultendo/agent-notify). Questo progetto è focalizzato sulla consegna ntfy durevole e offline per Codex attraverso più host. Vedere [docs/alternatives.md](docs/alternatives.md).

## Licenza

[MIT](LICENSE) © 2026 Riccardo Ravello e contributors.
