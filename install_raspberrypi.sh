cd /usr/src/app && \
python3 -m venv venv && \
source venv/bin/activate && \
sudo apt-get install libatlas-base-dev && \
pip3 install -r requirements.txt
sudo ln -s /usr/src/app/auto_trading.service /etc/systemd/system && \
systemctl daemon-reload
sudo systemctl start   auto_trading
sudo systemctl enable  auto_trading
