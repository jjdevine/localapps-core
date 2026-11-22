# Documentation

| Page | What it covers | Read it when |
| --- | --- | --- |
| [01 Wiring an app](01-wiring-an-app.md) | The few lines each part of the package needs in an app, file by file, and how an app's tests isolate themselves | Starting a new app, or moving an existing one onto the package |
| [02 How it works](02-how-it-works.md) | The login and its cookie, where secrets live, migrations at start-up, the health check, backups and restore | Before changing the package, or when its behaviour is in question |
| [03 Push notifications](03-push-notifications.md) | The recipe for adding notifications to an app (the table, the `Push` object, the routes, the service worker, the Settings section, tests), then how they work: the key, devices, sending, the scripts | Adding notifications to an app, moving an app's own copy onto the package, or changing how they are delivered |
| [04 The Claude API](04-the-claude-api.md) | The recipe for an app that asks Claude for something (the `Claude` object, the client, the Settings form for the key, the models, tests), then how the key is kept and what the package leaves to the app | Adding a call to the Claude API to an app, or moving an app's own key handling onto the package |

These pages change in the same piece of work as the code they describe.
