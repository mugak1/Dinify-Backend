from dataclasses import dataclass
from orders_app.controllers.initiate_order import initiate_order


@dataclass
class Order:
    data: dict

    def initiate_order(self):
        """
        Initiates a new order
        """
        return initiate_order(self.data)
