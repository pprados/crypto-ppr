Ce projet perso, cherche à permettre le trading automatique par des robots codé en Python.

Avec un processus qui tourne sur un raspberry, un docker ou une VM, il faut pouvoir 
executer des bots variées, codées en Python.

La difficulté majeur est que ces derniers doivent être *super résiliente*. C'est
à dire qu'il faut coder de tel manière qu'un crash à tout moment, ne plante pas les
bots. On doit pouvoir couper le courant à tout moment, sans impact.

Pour faire cela, chaque action doit être inscrite avant d'être exécutée.
En cas de plantage, au redemarrage du programme, il faut re-synchroniser le robot
pour reprendre le job là ou il s'est arreté.

Par exemple, pour créer un ordre, l'API Binance peut être invoqué. Mais, rien ne
garantie que le code recevra l'acquittement de la création de l'ordre. En cas
de plantage, il faut reprendre le contexte, puis s'assurer que l'ordre a bien
été passé, ou sinon, l'appliquer à nouveau.

# Création des API de tests:
Suivre la procédure [ici](https://dev.binance.vision/t/binance-testnet-environments/99/3)
pour créer des API classique et Future et les placer
dans un .env
```
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
BINANCE_API_TEST=true
```
Il est a noter que les API sont soit pour Spot, soit pour Future, mais pas les deux.

Il n'y pas de testnet pour Margin.

Interface Testnet future : https://testnet.binancefuture.com/en/futures/BTCUSDT


# Démarrage des bots
Pour pouvoir gérer plusieurs bots en parallèle, le fichier `conf.json`
permet d'indiquer la fonction async à appliquer pour le robot. 
On peut en ajouter autant que l'on souhaite.
```
[
  {
    "long_strategy": { # Nom du bot
      ... # Paramètre du robot
    }
  },
  {
    "smart_trade_1": { # Nom du bot
      "function": "smart_trade" # Optionel: Nom du module python avec la fonction bot()
      ... # Paramètre du robot
    }
  },
  {
    "smart_trade_2": { # Nom du bot
      "function": "smart_trade.bot" # Optionel: nom de la fonction à invoquer pour démarrer le robot
      ... # Paramètre du robot
    }
  }
]
```
Par défaut, la fonction correspond à la fonction 'bot()' dans le module 'nom_du_boot'.

# Automate
La bonne façon de coder un bot est de créer un automate à état, qui sauve son contexte
régulièrement, a chaque transition. Lors du redémarrage du programme,
le bot récupère le context. Il sait alors où il en est, et peut reprendre
le boulot. Parfois, il faut interroger Binance peut se resynchroniser (chercher l'état d'un ordre, etc).

## Persistance des états
Les contextes sont généralement sauvés en json. Pour simplifier le code, il est conseiller de créer une classe
permettant un accès via des propriétés, à la place des clés d'un dictionnaire.
Cela peut se faire simplement en héritant de `BotGenerator`.

```
class MonBot(BotGenerator)
    async def generator(self,
                        client: AsyncClient,
                        engine: 'Engine',
                        queue: Queue,
                        log: logging,
                        init: Dict[str, str],  # Initial context
                        client_account: Dict[str, Any],
                        generator_name: str,
                        conf: Dict[str, Any],
                        **kwargs) -> None:
```
Au début du bot, le code cherche à lire le contexte.
S'il n'est pas présent, c'est que le bot démarre pour la première fois. 

Il faut sauver l'état à chaque modification d'état, pour être capable de reprendre le job.
```
atomic_save_json(bot_generator, path)
```


## Librairie
Si ou souhaite faire des librairies avec des sous-automates, il faut utiliser des 
générateurs asynchrone. Avec un `yield`, ils retournent le contexte qui doit être sauvé
dans le contexte du robot. Regardez le code de `add_order` par exemple.
Ainsi, il est possible de gérer la forte résilience, tout en organisant le code
en modules réutilisables.

Il est également possible de lancer plusieurs ordres en parallèle, avec différents contextes.
C'est le bot qui se  charge de sauver son état, et donc, les états des différentes librairies.
Pour faire avance un générateur, 
```
ctx.order_ctx = await anext(current_order)
```
Il est ensuite possible de consulter l'état du context de la librairie, pour savoir où elle en est.
```
if ctx.order_ctx.state == ...
```

## Stream
La queue `bot_queue` récupère l'ensemble des messages de communications asynchrones,
avec les messages venant de Binance, et ceux des autres bots.

Comme il n'est pas possible d'ajouter un flux après la création, la stratégie utilisée
consiste à attendre que tous les agents se soient enregistrée avant de démarrer les flux.
(TODO: A revoir)


Attention, il est fort possible de perdre un message user !
En effet, il y a un mécanisme de reconnection du flux en cas de perte de ce dernier.
Pendant cette phase, il est possible que des évènements soient perdu pour le code.
D'autre part, lorsque le code redémarre, des messages peuvent également avoir été perdu.

Dans ce cas, une approche en pooling est à privilégier. Un mixte est possible.
Commencer par un pooling, puis en stream.
Par exemple, attendre des messages, et en plus, régulièrement, faire du polling.
Ainsi, le rebot réagit au plus vite, si possible. Sinon, sur timer.

## Sauvegarde des contextes
Pour sauver les contextes, il faut utiliser les fonctions atomic_save_json() et atomic_load_json()
afin de gérer le cas d'un crash lors de la manipulation des fichiers.

# Utilisation de python-binance
Tuto: https://algotrading101.com/learn/binance-python-api-guide/
Il faut valoriser les variables d'environements suivantes:
```
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
BINANCE_API_TEST=true
```
Cela peut être valorisé dans un fichier `.env`

# TODO
- buy range oro (donner top/down du range, puis un %. Place un ordre ORO a x% au dessus et au dessous du range)
- grid bot
- smart buy, sell, ...

# Service
https://github.com/torfsen/python-systemd-tutorial
sudo apt-get install -y python3-sdnotify

systemctl --user start    auto-trading
systemctl --user status   auto-trading
systemctl --user stop     auto-trading
systemctl --user restart  auto-trading
journalctl --user-unit    auto-trading
systemctl --user daemon-reload

systemctl --user daemon-reload ; systemctl --user restart   auto-trading ; journalctl --user-unit    auto-trading


# Telegram
https://www.geeksforgeeks.org/send-message-to-telegram-user-using-python/

## Installation Raspberry PI
- Création de l'image
rpi-imager
- ajouter un fichier ssh vide à la racine du boot

  
- Connexion pour changement password (via une connexion Ethernet)
ssh -o PreferredAuthentications=password pi@192.168.0.71 
passwd
  
- Copie de clé ssh pour connection
ssh-copy-id -i ~/.ssh/id_rsa.pub -o PreferredAuthentications=password pi@192.168.0.71
ssh pi@192.168.0.71

sudo raspi-config       # mise à jour wifi, vnc, etc
sudo apt full-upgrade   # Mise à jour OS
sudo reboot

sudo apt-get update
sudo apt-get install libffi-dev
  
- Copie des fichiers
sudo mkdir /usr/src/app  # Idem que sous Docker 
sudo chown pi:pi /usr/src/app
CTRL-D  
rsync -av -e ssh --exclude='venv' * pi@192.168.0.71:/usr/src/app
rcp .env pi@192.168.0.71:/usr/src/app
  
## Mise à jour de Python
Voir [ici](https://angorange.com/raspberry-pi-installation-de-python-3-9-1)
et en faire la version par défaut.

# Instalation des dépendences
ssh pi@192.168.0.71 "cd /usr/src/app && \
python3 -m venv venv && \
source venv/bin/activate && \
sudo apt-get install libatlas-base-dev && \
pip3 install -r requirements.txt"

- Vérifier le démarrage en local
ssh pi@192.168.0.71 "cd /usr/src/app && \
  source venv/bin/activate && \
  python3 auto_trading.py"

## En faire un service
ssh -p 8072 pi@88.124.108.99 "sudo ln -s /usr/src/app/auto_trading.service /etc/systemd/system && \
sudo systemctl daemon-reload && \
sudo systemctl status  auto_trading"

- Vérifier que cela fonctionne
ssh -p 8072 pi@88.124.108.99
sudo systemctl start  auto_trading
sudo systemctl status  auto_trading
sudo journalctl --unit=auto_trading -f
sudo systemctl stop  auto_trading
  
- Activer au reboot
sudo systemctl enable  auto_trading

## Mise à jour
rsync -av -e ssh --exclude='venv' * pi@192.168.0.71:/opt/auto_trading

# Notes
Type d'ordres https://www.binance.com/en/support/articles/360033779452

- https://www.starlette.io/responses/#streamingresponse
  https://www.starlette.io/responses/#sseresponseeventsourceresponse
- Calcul de la volatilité voir http://boursegestionportefeuille.e-monsite.com/pages/calcul-de-volatilite-bourse.html
