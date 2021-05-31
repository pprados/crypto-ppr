Ce projet perso, cherche à permettre le trading automatique.
Avec un processus qui tourne sur un raspberry ou un docker, il faut pouvoir 
executer des statégies variées, codées en Python.

La difficulté majeur est que ces dernières doivent être "super résiliente". C'est
à dire qu'il faut coder de tel manière qu'un crash à tout moment, ne plante pas les
stratégies.

Pour faire cela, chaque action doit être inscrite avant d'être exécuté.
En cas de plantage, au redemarrage du programme, il faut re-synchroniser la stratégie
pour reprendre le job là ou il s'est arreté.

Par exemple, pour créer un ordre, l'API Binance peut être invoqué. Mais, rien ne
garantie qu'on recevra l'acquittement de la création de l'ordre. En cas
de plantage, il faut reprendre le contexte, puis s'assurer que l'ordre a bien
été passé, ou sinon, l'appliquer à nouveau.

Pour pouvoir gérer plusieurs stratégies en parallèle, le fichier conf.json
permet d'indiquer la fonctione async à appliquer pour la stratégie. 
On peut en ajouter autant que l'on souhaite.

La bonne façon de coder est de créer un automate à état, qui sauve sont contexte
régulièrement, a chaque transition. Lors du redémarrage du programme,
l'agent récupère le context. Il s'est alors où il en ait, et peut reprendre
le boulot. Parfois, il faut interroger Binance peut se resynchroniser.

Si ou souhaite faire des librairies avec un sous-automate, il faut utiliser des 
générateur asynchrone. Ils retournent le contexte qui doit être sauvé
dans le contexte de l'agent. Regardez le code de filled_order.

Ainsi, il est possible de gérer la forte résilience, tout en organisant le code
en modules.
Il est également possible de lancer plusieurs ordres en parallèle, avec différents contextes.

Les différents agents peuvent communiquer entre-eux, via des messages asynchrones.
Une queue est dispo pour chaque agent.

Pour récuperer les flux asynchrones de Binance, il faut, au tout début de l'agent
enregistrer un call-back sur les multiplex ou les évenements users.

Comme il n'est pas possible d'ajouter un flux après la création, la stratégie utilisée
consiste à attendre que tous les agents se soient enregistrée avant de démarrer les flux.

En générale, dans un agent, la call-back consiste simplement à déposer le message
dans une queue locale à l'agent.

```
user_queue = asyncio.Queue()  # Queue to receive event from user account
add_user_socket(lambda msg: user_queue.put_nowait(msg))
```
ainsi, l'agent peut attendre un message
```
msg = await user_queue.get()
```

Attention, il est fort possible de perdre un message user ! 
Dans ce cas, une approche en pooling est à privilégier. Un mixte est possible.
Commencer un pooling, puis en stream, jusqu'a une erreur.

Pour sauver les contextes, il faut utiliser les fonctions atomic_save_json() et atomic_load_json()
afin de gérer le cas d'un crash lors de la manipulation des fichiers.