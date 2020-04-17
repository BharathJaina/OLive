# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

# install vue packages
cd frontend
npm install
cd ..
# install python packages
python3 -m pip install flask flask-cors redis celery[redis] flower gevent

# pull docker images
sh ../utils/build.sh
