import select
import socket
import struct
from typing import Callable, Dict, List, Union

from events import ClientEvent, ServerEvent, ClientEventName, ServerEventName
from map_generator import MapGenerator
from planet import Planet
from player import Player
from utils import EventPriorityQueue, StoppedThread, ID_GENERATOR

MAX_CLIENT_COUNT = 8


def create_tcp_server(server_address: Union[tuple, str, bytes], backlog: int, blocking: bool = False) -> socket.socket:
    server_socket = socket.socket(type=socket.SOCK_STREAM)
    server_socket.setblocking(blocking)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(server_address)
    server_socket.listen(backlog)
    return server_socket


class Server(object):
    RECEIVER_TIMEOUT = 10  # seconds

    def __init__(self, port: int = 10800, max_client_count: int = MAX_CLIENT_COUNT):
        self.server = create_tcp_server(('127.0.0.1', port), max_client_count)
        self.started = True
        self.clients: List[socket.socket] = []
        self.__max_clients_count = max_client_count
        self.players: Dict[socket.socket, Player] = {}
        self.next_player_id = 0
        self.readiness = False
        self.game_started = False

        self.__handler_queue = EventPriorityQueue()
        self.__sender_queue = EventPriorityQueue()

        self.__threads: List[StoppedThread] = []

    def __start_thread(self, name, callback, args=None):
        t = StoppedThread(name=name, target=callback, args=args)
        self.__threads.append(t)
        t.start()

    def start(self):
        self.__start_thread(name='receiver', callback=self.__receiver, args=())
        self.__start_thread(name='handler', callback=self.__handler, args=())
        self.__start_thread(name='sender', callback=self.__sender, args=())

    def stop(self):
        for thread in self.__threads:
            thread.stop()
            thread.join()

    def __wait_for_client(self, server_sock):
        if not self.game_started and not self.readiness and len(self.clients) < self.__max_clients_count:
            client, address = server_sock.accept()
            client.setblocking(0)

            self.clients.append(client)
            self.next_player_id += 1

            player = Player(address, self.next_player_id)
            self.players[client] = player

            self.__notify(ServerEventName.CONNECT, {
                'player': player.info()
            })

    def __remove_client(self, client_sock):
        self.clients.remove(client_sock)
        del self.players[client_sock]
        client_sock.close()

    def __wait_for_client_data(self, client_sock):
        data_size = client_sock.recv(struct.calcsize('i'))

        if data_size:
            data_size = struct.unpack('i', data_size)[0]
            event = client_sock.recv(data_size)

            if event:
                player = self.players[client_sock]
                event = ClientEvent(player, event)
                self.__handler_queue.insert(event)
            else:
                self.__remove_client(client_sock)
        else:
            self.__remove_client(client_sock)

    def __receiver(self, is_alive: Callable):
        while is_alive():
            readable, *_ = select.select([self.server, *self.clients], [], [], self.RECEIVER_TIMEOUT)

            for sock in readable:
                if sock is self.server:
                    self.__wait_for_client(sock)
                else:
                    self.__wait_for_client_data(sock)

    def __sender(self, is_alive: Callable):
        while is_alive():
            if not self.__sender_queue.empty():
                event = self.__sender_queue.remove()

                for client in self.clients:
                    client.send(event.request())

    def __notify(self, name: ServerEventName, args: dict):
        self.__sender_queue.insert(ServerEvent(name, args))

    def __handler(self, is_alive: Callable):
        while is_alive():
            if not self.__handler_queue.empty():
                event = self.__handler_queue.remove()
                player = event.payload['player']

                if event.name == ClientEventName.READY:
                    player.ready = event.payload['ready']

                    self.__notify(event.name, {
                        'player': player.id,
                        'ready': event.payload['ready'],
                    })

                    all_ready = all(player.ready for player in self.players.values())

                    if all_ready and len(self.players) > 1:
                        gen = MapGenerator()
                        map_ = gen.run([player.id for player in self.players.values()])
                        gen.display()
                        game_map = [planet.get_dict() for planet in map_]

                        self.readiness = True

                        self.__notify(ServerEventName.MAP_INIT, {
                            'map': game_map
                        })

                elif event.name == ClientEventName.RENDERED:
                    player.rendered = True

                    if all(player.rendered for player in self.players.values()):
                        self.__notify(ServerEventName.GAME_STARTED, {})
                        self.game_started = True

                if self.game_started:
                    if event.name == ClientEventName.MOVE:
                        if int(event.payload['unit_id']) in player.object_ids:
                            self.__notify(event.name, event.payload)

                    elif event.name == ClientEventName.SELECT:
                        planet_ids = event.payload['from']
                        percentage = event.payload['percentage']

                        punits = {}

                        for planet_id in planet_ids:
                            planet_id = int(planet_id)

                            if Planet.cache[planet_id].owner == player.id:
                                new_ships_count = round(Planet.cache[planet_id].units_count * int(percentage) / 100.0)
                                Planet.cache[planet_id].units_count -= new_ships_count
                                punits[planet_id] = [next(ID_GENERATOR) for _ in range(new_ships_count)]
                                player.object_ids += punits[planet_id]

                        self.__notify(event.name, {
                            'selected': punits
                        })

                    elif event.name == ClientEventName.ADD_HP:
                        planet_id = int(event.payload['planet_id'])
                        hp_count = int(event.payload['hp_count'])

                        planet = Planet.cache[planet_id]

                        if planet.owner == player.id:
                            planet.units_count += hp_count
                            self.__notify(event.name, event.payload)

                    elif event.name == ClientEventName.DAMAGE:
                        planet_id = int(event.payload['planet_id'])
                        unit_id = int(event.payload['unit_id'])
                        hp_count = int(event.payload.get('hp_count', 1))

                        planet = Planet.cache[planet_id]

                        if unit_id in player.object_ids:
                            if planet.owner == player.id:
                                planet.units_count += hp_count
                            else:
                                planet.units_count -= hp_count
                                if planet.units_count < 0:
                                    planet.owner = player.id
                                    planet.units_count = abs(planet.units_count)

                            player.object_ids.remove(unit_id)

                            self.__notify(event.name, {
                                'planet_change': {
                                    'id': planet_id,
                                    'units_count': planet.units_count,
                                    'owner': planet.owner,
                                },
                                'unit_id': unit_id,
                            })

                        # check game over

                        active_players = []

                        for player in self.players.values():
                            if len(player.object_ids) > 0:
                                for planet in Planet.cache.values():
                                    if planet.owner == player.id:
                                        active_players.append(player.id)
                                        break
                            if len(active_players) >= 2:
                                break
                        else:
                            self.__notify(ServerEventName.GAME_OVER, {
                                'winner': active_players[0],
                            })

                            self.readiness = False
                            self.game_started = False

                            self.players = {}
                            self.clients = []