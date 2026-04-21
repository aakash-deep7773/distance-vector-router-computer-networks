# Distance Vector Router (Docker-Based)

## 📌 Overview

This project implements a custom **Distance Vector Routing Protocol** (similar to RIP) using Python.  
Routers are simulated using Docker containers and communicate using UDP sockets to dynamically learn network paths.

The system automatically calculates shortest paths using the **Bellman-Ford algorithm** and updates routes when the network topology changes.

---

## ⚙️ Features

- Distance Vector routing using Bellman-Ford algorithm  
- UDP-based communication between routers (port 5000)  
- JSON-based routing updates (DV-JSON format)  
- Dynamic routing table updates  
- Split Horizon to prevent routing loops  
- Automatic route recalculation on failure  
- Docker-based network simulation  

---

## 🏗️ Network Topology

The network consists of 3 routers connected in a triangle:

- Router A  
- Router B  
- Router C  

Each router connects to two networks:
- net_ab (A ↔ B)  
- net_bc (B ↔ C)  
- net_ac (A ↔ C)  

---

## 📂 Project Structure

```
distance-vector-router-docker/
│
├── router.py          # Main routing logic
├── Dockerfile         # Docker image configuration
├── LAB_COMMANDS.md    # Commands to run the project
└── README.md          # Project documentation
```

---

## 🚀 How to Run

Follow the steps in `LAB_COMMANDS.md`:

1. Create Docker networks  
2. Build the Docker image  
3. Run Router A, B, and C  
4. Verify using:
   ```
   docker ps
   docker logs router_a
   ```
5. Check routing table:
   ```
   docker exec -it router_a sh
   ip route
   ```

---

## 🔍 Testing

### Normal Case
- Routers exchange routing updates  
- Shortest paths are calculated automatically  

### Failure Case
- Stop Router C:
  ```
  docker stop router_c
  ```
- Wait ~20 seconds  
- Routers update paths dynamically  
- Traffic reroutes via Router B  

---

## 🧠 Concepts Used

- Bellman-Ford Algorithm  
- Distance Vector Routing  
- Split Horizon  
- UDP Communication  
- Docker Networking  
- Linux Routing Table (`ip route`)  

---

## 📸 Output

The project demonstrates:
- Dynamic route learning  
- Network convergence  
- Fault tolerance  

---

## 🎯 Conclusion

This project successfully simulates a real-world routing protocol where routers:
- Learn paths dynamically  
- Adapt to failures  
- Maintain updated routing tables  

It provides practical understanding of routing algorithms and network behavior.

---

## 👤 Author

Aakash Deep

---
