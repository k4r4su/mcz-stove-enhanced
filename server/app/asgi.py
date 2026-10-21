import logging

from .main import create_app

# Messages « mcz » visibles dans `docker logs` (commandes, planificateur), sans le bruit des autres bibliothèques.
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
logging.getLogger("mcz").setLevel(logging.INFO)

app = create_app()
