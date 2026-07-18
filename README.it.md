# Codex ntfy Notifier — notifiche di completamento idle-aware

[![CI](https://github.com/ravhello/codex-ntfy-notifier/actions/workflows/ci.yml/badge.svg)](https://github.com/ravhello/codex-ntfy-notifier/actions/workflows/ci.yml)
[![Ultima release](https://img.shields.io/github/v/release/ravhello/codex-ntfy-notifier?display_name=tag&sort=semver)](https://github.com/ravhello/codex-ntfy-notifier/releases/latest)
[![Licenza: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Windows PowerShell 5.1](https://img.shields.io/badge/Windows%20PowerShell-5.1-5391FE.svg)](https://learn.microsoft.com/powershell/)

[English](README.md) · [Architettura](docs/architecture.md) · [Privacy e sicurezza](docs/security-and-privacy.md) · [Supporto](SUPPORT.md) · [Alternative](docs/alternatives.md)

Una sola notifica push ntfy compatta quando una task root locale di OpenAI Codex è verificabilmente inattiva, non a ogni risultato intermedio. Supporta anche Claude Code su Windows, inclusa la scheda Code di Claude Desktop, tramite hook lifecycle nativi opzionali.

![Codex ntfy Notifier aspetta l'idle verificabile localmente prima di inviare una sola notifica di completamento compatta](docs/assets/hero.svg)

> [!IMPORTANT]
> Questo è un progetto community non ufficiale, non affiliato né approvato da OpenAI, Anthropic o ntfy.

## Cosa lo distingue

- **Idle-aware:** la task root deve risultare inattiva da prove locali; turni intermedi, goal attivi e subagent ancora in esecuzione mantengono la notifica in attesa.
- **Consegna durevole:** outbox atomica, deduplicazione stabile e retry con backoff forniscono consegna at-least-once dopo la conferma di idle.
- **Multi-ambiente:** app Codex, VS Code, CLI, Windows, WSL, Linux nativo, installazioni Remote SSH locali all'host e Claude Code opzionale su Windows condividono lo stesso motore di consegna durevole.
- **Recovery rapido e isolato:** su Windows lo scanner locale persistente segue le entry recenti di Codex in SQLite invece di ripercorrere ogni volta tutto l'archivio delle sessioni; il recovery UNC/WSL gira separatamente e non blocca la consegna locale. Le installazioni Remote SSH mantengono worker e coda sull'host remoto.
- **Privacy predefinita:** prompt e messaggi finali sono esclusi; titolo della task, estratto del messaggio e percorso completo richiedono ciascuno un opt-in esplicito.

## Avvio rapido

Il percorso è **installazione → `/hooks` → doctor → test**. Ogni ambiente reale Windows, WSL, Linux o SSH ha un proprio `CODEX_HOME` e deve essere installato separatamente.

Prima di installare, [sottoscrivere con un client ntfy](https://docs.ntfy.sh/subscribe/phone/) un topic difficile da indovinare oppure protetto dal [controllo accessi ntfy](https://docs.ntfy.sh/config/#access-control). Serve anche Git; Linux nativo richiede Python 3.10 o successivo.

### 1a. Installazione su Windows e WSL

Aprire PowerShell:

```powershell
git clone https://github.com/ravhello/codex-ntfy-notifier.git
cd codex-ntfy-notifier
.\install.ps1 -WslDistro Ubuntu
```

Per il solo Windows usare `.\install.ps1 -NoWsl`. Dopo l'installazione, ricaricare Codex e le finestre VS Code già aperte.

Per collegare anche Claude Code su Windows (scheda Code di Claude Desktop, CLI e VS Code), aggiungere l'opt-in esplicito:

```powershell
.\install.ps1 -WslDistro Ubuntu -EnableClaudeCode
```

L'installer unisce atomicamente in `~/.claude/settings.json` `Stop`/`StopFailure` e `UserPromptSubmit` ordinati e sincroni, più gli acceleratori `Notification` asincroni opzionali (`idle_prompt` e `agent_completed`). Conserva gli altri hook Claude e include l'originale nel backup Codex. Serve Claude Code 2.1.198 o successivo per l'intero set lifecycle gestito. L'installer controlla separatamente l'eseguibile più recente trovato per ogni superficie rilevata—`PATH`, Claude Desktop, VS Code, VS Code Insiders e Cursor—e si interrompe se una superficie rilevata è più vecchia di quel minimo.

### 1b. Installazione su Linux nativo

```sh
git clone https://github.com/ravhello/codex-ntfy-notifier.git
cd codex-ntfy-notifier
CODEX_NTFY_TOPIC=$(python3 -c 'import getpass; print(getpass.getpass("Topic ntfy privato: "))')
export CODEX_NTFY_TOPIC
./install-linux.sh
unset CODEX_NTFY_TOPIC
```

### 2. Revisione dell'hook

In ogni ambiente Codex installato, eseguire `/hooks`, controllare il comando `Stop` gestito e approvarlo. L'installer non modifica mai il trust store di Codex.

In Claude Code `/hooks` è un visualizzatore in sola lettura: verificare quattro tipi di evento gestiti e cinque handler (`Notification` ha handler separati per `idle_prompt` e `agent_completed`). Claude normalmente ricarica automaticamente `settings.json`.

### 3. Esecuzione del doctor

Windows:

```powershell
& "$HOME\.codex\notify-ntfy.ps1" -Doctor
```

Linux o WSL:

```sh
python3 ~/.codex/notify-ntfy.py --doctor
```

### 4. Invio di una notifica di test

Windows:

```powershell
& "$HOME\.codex\notify-ntfy.ps1" -Test
```

Linux o WSL:

```sh
python3 ~/.codex/notify-ntfy.py --test
```

> Se risolve un problema reale nel tuo flusso di lavoro, puoi [lasciare una stella alla repository](https://github.com/ravhello/codex-ntfy-notifier).

## Perché esiste

Una singola task Codex può produrre più segnali di fine turno mentre ha ancora lavoro: può partire subito una continuazione automatica, il goal può essere ancora `active` oppure un subagent può stare lavorando. Inviare ogni segnale crea notifiche “completato” premature.

La versione 2.4 ha introdotto il **periodo di idle logico** mantenuto dalla 2.5:

- l’hook moderno Codex `Stop` crea un candidato, ma non pubblica direttamente;
- la notifica legacy `agent-turn-complete` rimane come segnale di compatibilità;
- il watcher continuo dei rollout recupera completion perse dagli hook quando osserva lo stesso `CODEX_HOME`;
- su Windows il watcher locale persistente ottiene i rollout attivi o ripresi di recente dall'indice SQLite Codex in sola lettura e controlla soltanto i percorsi correnti più caldi; il percorso continuo non ripete scansioni ricorsive di alberi `sessions/` e `archived_sessions/` da molti gigabyte;
- il recovery fallback UNC/WSL usa uno scanner separato con timeout, quindi una distro sospesa o una share lenta non ritarda lo scanner locale né una consegna ntfy già pronta;
- ogni candidato entra prima in `pending/`;
- l’idle gate verifica che lo stesso turno sia completo, che non sia iniziato un turno successivo, che il goal non sia più attivo, che i discendenti abbiano finito e che il rollout sia rimasto quieto per una breve finestra; su Windows un parser strutturale nativo restituisce un riepilogo lifecycle di dimensione fissa invece di rileggere un rollout grande riga per riga in PowerShell;
- i candidati ancora pending della stessa chat root vengono consolidati; una completion seguita da una task successiva ancora aperta viene soppressa come predecessore superato, mentre un’epoca già promossa nell’outbox resta immutabile;
- la modalità predefinita `strict` non fa mai fail-open: ritenta le prove mancanti per `idle_probe_grace_seconds`, poi sopprime localmente un candidato non verificabile invece di annunciare prematuramente la fine.

La versione 2.5 aggiunge un percorso Claude specifico su Windows. `Stop` viene accettato solo per l'agente principale quando entrambi i registri di lavoro autorevoli sono presenti e vuoti; `session_id + prompt_id` garantisce la deduplicazione e `StopFailure` copre i turni terminati da un errore API. `Stop`, `StopFailure` e `UserPromptSubmit` sono ordinati e sincroni, così gli stop ripetuti dello stesso goal non possono concludersi fuori ordine; la scansione iniziale è limitata a 1 MiB e l'eventuale riconciliazione completa avviene nel worker. `UserPromptSubmit` fotografa il marker goal precedente e annulla i candidati obsoleti prima che possa terminare il nuovo prompt. Il gate replica poi la regola di ripristino di Claude dal più recente `attachment.goal_status` locale: `active`/non raggiunto mantiene il candidato in attesa, un marker successivo raggiunto o fallito lo libera, mentre il sentinel di cancellazione manuale lo elimina senza notifica. `idle_prompt`/`agent_completed` con lo stesso `prompt_id` non vuoto sono solo fallback asincroni opzionali: eventi idle lenti o non correlabili in VS Code non possono liberare il candidato sbagliato. Il corpo usa comunque il messaggio finale fornito da Claude.

Dopo la conferma di idle, il motore di consegna:

- sposta atomicamente l’evento nell’outbox prima di usare la rete;
- ritenta gli errori transitori con backoff e jitter;
- deduplica tramite `thread-id + turn-id` e riusa un `sequence_id` ntfy stabile;
- isola i record corrotti senza bloccare la coda;
- non memorizza i prompt e, per impostazione predefinita, esclude anche il messaggio finale.

La garanzia di consegna è **at-least-once durevole**, non exactly-once transazionale.

## Titolo minimo della notifica

Dalla versione 2.4.2, la regola idle-only della 2.4 usa un titolo senza prefissi ridondanti:

```text
Titolo visibile: ✅ <conversazione-o-progetto>
Corpo:  [messaggio finale ·] [progetto ·] origine · #thread8
```

Il titolo visibile è composto esattamente da una sola emoji di completamento/stato resa da ntfy e dal titolo locale della conversazione, oppure dalla directory progetto quando la condivisione del titolo è disattivata o non disponibile. I titoli Codex provengono dal database di stato in sola lettura o dall'indice sessioni; quelli Claude dai metadati transcript `ai-title`/`custom-title` letti con un limite. L'unico tag predefinito `white_check_mark` fornisce l'emoji: il notifier non aggiunge `Codex`, `Claude`, `done`, il nome del modello, uno stato testuale o altre emoji decorative.

Con il default `markdown: false`, il corpo occupa una sola riga e il contesto non usa etichette come `Project:`, `Source:` o `Thread:`. Con il default privacy `include_message: false` contiene soltanto il progetto necessario (quando non è già nel titolo), l'origine e `#` seguito dai primi otto caratteri dell'ID della chat. Con `include_message: true` viene anteposto un estratto redatto del messaggio finale: il Markdown di presentazione viene ridotto a testo semplice compatto, conservando le etichette dei link e il testo delle celle, e `max_message_chars` vale 180 per default. L'intero campo ntfy `message` ha comunque un limite rigido di 3.500 byte UTF-8. L'opt-in esplicito `markdown: true` conserva il Markdown e le righe dell'estratto opzionale.

Per default il tap non esegue azioni aggiuntive. Solo per Codex, `include_task_link: true` aggiunge l'URL HTTPS autenticato `https://chatgpt.com/codex/tasks/<thread-id>` come [destinazione ntfy `click`](https://docs.ntfy.sh/publish/#click-action). Le notifiche Claude omettono intenzionalmente quell'URL ChatGPT: Claude non documenta un deep link per riaprire una sessione Code locale esistente.

Le nuove installazioni usano un solo tag ntfy, `white_check_mark`. Oltre all'emoji resa da quel tag, i template non aggiungono emoji decorative nel titolo o nel corpo. Markdown è disattivato e la priorità predefinita 3 viene rappresentata omettendo `priority` dal JSON in uscita. Una priorità personalizzata diversa da 3 viene invece inviata esplicitamente.

## Ambienti supportati

| Ambiente | Segnali di completion | Worker |
| --- | --- | --- |
| Windows 10/11 | `Stop` moderno + `notify` legacy + watcher rollout | Utilità di pianificazione |
| Claude Code su Windows | `Stop`/`StopFailure` agente principale, avvio prompt ordinato e gate goal dal transcript; lavori attivi e loop `/goal` fanno fail-closed | stesso worker Windows |
| WSL2 | segnali Linux, bridge Windows, root rollout registrata, fallback nativo | worker Windows / Python |
| Linux nativo | `Stop` moderno + `notify` legacy + watcher rollout | servizio systemd utente / on-demand |
| Remote SSH Windows/Linux | segnali e stato rollout dell’host remoto | worker sull’host remoto |

Le stesse regole valgono per task locali avviate da app Codex, VS Code o CLI quando quel processo Codex scrive hook e rollout nell’ambiente installato. Windows, ogni distribuzione WSL, Linux e ogni host SSH hanno un proprio `CODEX_HOME` e vanno configurati separatamente.

Le task interamente cloud che non replicano lo stato nel `CODEX_HOME` locale non sono garantite. Il progetto non si collega a stream privati dell’interfaccia.

Fonti ufficiali: [Codex Hooks](https://learn.chatgpt.com/docs/hooks), [configurazione delle notifiche](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications) e [hook Claude Code](https://code.claude.com/docs/en/hooks). Il record locale `attachment.goal_status` è un dettaglio del transcript Claude usato in modo difensivo per la continuità di `/goal`, non un campo dichiarato del payload hook.

## Dettagli dell'installazione Windows e WSL

L'avvio rapido Windows/WSL riportato sopra chiede il topic con input nascosto su una nuova installazione. L’installer:

1. crea la configurazione privata;
2. salva un backup di rollback;
3. rimuove soltanto i record locali creati esplicitamente dal comando di test sintetico del notifier;
4. installa il worker `CodexNtfyWatcher`; Utilità di pianificazione avvia direttamente il supervisore VBS nascosto, evitando due avvii PowerShell a freddo prima che il notifier sia pronto;
5. conserva o installa `notify` come fallback legacy;
6. registra `hooks.Stop` senza sostituire handler non gestiti dal progetto;
7. con `-EnableClaudeCode`, unisce `Stop`/`StopFailure`/`UserPromptSubmit` ordinati e sincroni e gli handler `Notification` asincroni opzionali nelle impostazioni Claude e include quel file nel rollback;
8. installa bridge e fallback nativo WSL e registra nel watcher Windows le root Codex/SQLite della distribuzione.

Per Windows senza WSL:

```powershell
.\install.ps1 -NoWsl
```

### Revisione una tantum dell’hook

Codex non esegue automaticamente un nuovo hook finché l’utente non lo ha revisionato. In ogni ambiente installato, aprire `/hooks` e approvare l’hook `Stop` gestito dopo averne controllato il comando.

L’installer non modifica intenzionalmente il trust store di Codex. Prima dell’approvazione restano attivi il fallback legacy e il watcher dei rollout; l’hook moderno è comunque il candidato di stop più tempestivo, poi classificato dal notifier come root o discendente.

## Dettagli dell'installazione Linux

Con `CODEX_NTFY_SKIP_SYSTEMD=1` si usa soltanto il worker on-demand. Il worker continuo è consigliato perché esegue il watcher che recupera i segnali mancanti.

## Host Remote SSH

Gli installer remoti copiano destinazione, autenticazione e policy private, ma azzerano `watch_roots`: i percorsi WSL/custom del computer sorgente non sono portabili. Eventuali root aggiuntive vanno configurate sulla destinazione.

Windows, da PowerShell:

```powershell
.\install-remote-windows.ps1 -HostName my-windows-host
```

Linux, da Linux o WSL:

```sh
./install-remote-linux.sh my-linux-host "$HOME/.codex/ntfy-config.json"
```

Ogni host reale mantiene `pending/`, cursori dei rollout, outbox e worker propri. Quando possibile, usare un token ntfy publish-only diverso per ciascun host. Revisionare `/hooks` anche nell’ambiente remoto.

## Configurazione

La configurazione privata è `~/.codex/ntfy-config.json`; vedere [ntfy-config.example.json](ntfy-config.example.json).

### Rilevamento idle

| Impostazione | Default | Significato |
| --- | ---: | --- |
| `idle_detection_mode` | `"strict"` | `strict` non trasforma mai una prova mancante in notifica; `balanced` può usare il fallback temporale; `off` torna alla coda immediata per turno. |
| `idle_grace_seconds` | `1.5` | Quiet time richiesto dopo la completion corrispondente. |
| `idle_probe_grace_seconds` | `30` | Finestra di verifica: alla scadenza `balanced` può accettare prove sconosciute o non disponibili; UTF-8/lifecycle malformati e una riga JSONL finale parziale restano sempre fail-closed. `strict` sopprime localmente il candidato non verificabile senza inviarlo. |
| `unknown_retry_max_seconds` | `60` | Intervallo massimo tra i tentativi esponenziali quando la prova root o rollout resta sconosciuta. |
| `goal_aware` | `true` | Trattiene il candidato finché il goal root è `active`. |
| `goal_poll_seconds` | `1` | Intervallo di ricontrollo di goal, turno e discendenti. |
| `subagent_orphan_seconds` | `1800` | Età dopo la quale un rollout figlio fermo non blocca per sempre. |
| `suppress_technical_turns` | `true` | Sopprime completion legacy/watcher non rivolte all’utente; un `Stop` classificato come root resta candidato. |
| `watch_rollouts` | `true` | Recupera completion persistite localmente ma perse dagli hook. |
| `watch_scan_seconds` | `2` | Frequenza rapida per i rollout recenti modificati di recente. |
| `watch_discovery_seconds` | `60` | Frequenza di refresh dei cursor storici e della discovery limitata in directory vecchie o archiviate; i cursor invariati non vengono riscritti. |
| `watch_cursor_batch_size` | `64` | Numero Windows di rollout storici verificati per ciclo freddo; i metadati dei cursor impediscono comunque il replay della cronologia. |
| `watch_remote_timeout_seconds` | `90` | Limite Windows per una scansione fallback UNC/WSL isolata; un blocco remoto non rallenta recovery locale o consegna. |
| `watch_initial_replay_seconds` | `15` | Alla prima osservazione recupera solo una coda del rollout molto recente. |
| `watch_roots` | `[]` | Root Codex aggiuntive osservate dal worker Windows; `install.ps1` gestisce quelle delle distribuzioni WSL selezionate, con root SQLite e origine. |
| `worker_sqlite_path` | gestito dall'installer | Root SQLite locale usata dal watcher pianificato Windows se diversa da `CODEX_HOME`; gli installer remoti la reimpostano sulla destinazione. |

Usare `strict` quando evitare falsi “finito” è la priorità. Ritenta le prove sconosciute con intervalli esponenziali limitati, poi registra localmente `unverifiable` al termine della finestra senza inviarlo a ntfy. `balanced` privilegia la disponibilità dopo 30 secondi quando prove altrimenti valide restano sconosciute o non disponibili e può quindi produrre un falso positivo; non promuove mai UTF-8/lifecycle malformati o una riga JSONL finale parziale. `off` è una modalità di compatibilità/diagnostica.

### Privacy e consegna

- `include_message: false`: non conserva né invia il messaggio finale;
- `max_message_chars: 180`: limita l'estratto finale opzionale; il corpo completo resta comunque limitato a 3.500 byte UTF-8;
- `include_thread_title: false`: non usa il titolo della task, che può riassumere il prompt;
- `include_task_link: false`: non invia a ntfy l'URL della task con l'ID completo;
- `include_task_link_action: false`: non mostra un pulsante `view` separato; richiede comunque `include_task_link`;
- `include_full_path: false`: non aggiunge al corpo il percorso di lavoro completo sanitizzato;
- `tags: ["white_check_mark"]`: usa un solo tag ntfy senza duplicare un'emoji nel testo;
- `priority: 3`: usa la priorità ntfy predefinita e omette il campo dal JSON in uscita;
- `markdown: false`: invia il corpo compatto come testo semplice;
- `suppress_subagents: true`: non invia completion dei discendenti;
- `max_attempts: 0`: retry illimitati per errori transitori;
- `sent_retention_days: 14` e `dead_retention_days: 30`: retention di receipt e dead-letter;
- `allow_insecure_auth: false`: vieta credenziali su HTTP non locale.

Con `include_message: true`, il messaggio finale viene redatto e troncato, ma la redazione regex è solo best-effort. I prompt utente non vengono salvati.

`include_message` viene verificato di nuovo al momento dell'invio. Disattivarlo impedisce che il contenuto finale presente in record già accodati lasci l'host, ma non cancella il record locale, i backup, le dead letter, una richiesta già in corso o una notifica già accettata da ntfy. `include_full_path: true` resta un opt-in separato che può esporre il percorso di lavoro sanitizzato. `include_task_link: true` invia l'ID completo dentro un URL HTTPS ChatGPT, senza aggirare l'autenticazione. Il notifier usa il fallback HTTPS mobile/web invece dello [schema di compatibilità desktop `codex://`](https://learn.chatgpt.com/docs/reference/commands#deep-links).

L’idle gate legge metadati locali e campi SQLite in sola lettura. Interroga lo **stato** del goal, non il suo obiettivo. Il watcher conserva path, offset, timestamp e ID della chat, non il contenuto dei prompt. Con Claude attivo una scansione inversa a memoria limitata trova soltanto il più recente attachment lifecycle `goal_status` rilevante e conserva stato più marker opaco; non estrae, salva, registra nei log o invia la condizione/motivazione del goal.

## Diagnostica

Windows:

```powershell
& "$HOME\.codex\notify-ntfy.ps1" -Doctor
Get-ScheduledTask CodexNtfyWatcher | Select-Object TaskName, State
```

Linux/WSL:

```sh
python3 ~/.codex/notify-ntfy.py --doctor
systemctl --user status codex-ntfy.service
```

Nel doctor:

- `pending_idle` indica i candidati che aspettano ancora la prova di idle;
- `queued` indica gli eventi già pronti per la rete;
- `watched_rollouts` indica i cursori persistiti dal recovery watcher;
- `idle_detection_mode`, `goal_aware` e `watch_rollouts` mostrano la policy attiva.

Stato locale:

```text
~/.codex/ntfy-state/
  pending/      candidati in attesa dell’idle gate
  outbox/       eventi idle in attesa di ntfy
  watch/        cursori incrementali dei rollout
  sent/         receipt di consegna
  suppressed/   receipt subagent/tecniche/superate
  dead/         record invalidi o falliti definitivamente
  notify.log    log operativo limitato
```

Non cancellare `pending/` o `outbox/` durante un problema normale. Consultare [Risoluzione problemi](docs/troubleshooting.md).

## Limiti noti

- Gli hook moderni richiedono approvazione esplicita tramite `/hooks`.
- Il supporto Claude riguarda attualmente Claude Code locale su Windows. La normale scheda Chat di Claude non espone gli hook Code, un'interruzione manuale non emette `Stop` e il lavoro hosted senza hook locale non è osservabile.
- La finalità di Claude `/goal` dipende da una scansione inversa a memoria limitata dei record locali `attachment.goal_status`, senza caricare l'intero transcript. È un formato upstream che può richiedere un adattamento se Claude lo cambia; prove mancanti o malformate per un goal attivo fanno fail-closed invece di inviare un risultato intermedio.
- `strict` sopprime localmente come `unverifiable` una vera completion se, trascorsi `idle_probe_grace_seconds`, Codex non conserva più le prove necessarie.
- `balanced` può notificare dopo il grace period quando una prova altrimenti valida resta sconosciuta o non disponibile; dati lifecycle malformati o parziali restano fail-closed.
- I formati rollout e gli schemi SQLite locali appartengono a Codex e possono cambiare.
- Un figlio abbandonato smette di bloccare dopo `subagent_orphan_seconds`.
- Le task solo cloud non sono garantite senza stato locale.
- Il recovery autonomo richiede un worker continuo. L'installer Windows registra solo le distribuzioni passate a `-WslDistro`; le altre non vengono scandite implicitamente.
- La consegna è at-least-once dopo la conferma idle, non exactly-once.

## Alternative

Esistono notifier più piccoli e strumenti multi-agent/multi-provider. Questa repo è focalizzata su tre proprietà insieme: prova di idle della chat root, consegna ntfy durevole e topologia multi-host. Vedere [Alternative e progetti adiacenti](docs/alternatives.md).

## Test

La suite usa soltanto un server HTTP locale finto e non contatta un topic ntfy reale:

```sh
python3 -m unittest discover -s tests -v
```

## Sicurezza

Leggere [Privacy e sicurezza](docs/security-and-privacy.md) e segnalare vulnerabilità in privato come descritto in [SECURITY.md](SECURITY.md).

## Licenza

[MIT](LICENSE) © 2026 Riccardo Ravello e contributors.
