# Docker Lab Commands (Windows / VS Code Terminal) — FINAL (WORKING VERSION)

Run these from your project folder (where `Dockerfile` and `router.py` are present).

---

## 0. Prerequisites

- Docker Desktop running (Linux containers)
- VS Code terminal opened in project folder

---

## 1. Create Networks

```powershell
docker network create --subnet=10.0.1.0/24 net_ab
docker network create --subnet=10.0.2.0/24 net_bc
docker network create --subnet=10.0.3.0/24 net_ac
```

---

## 2. Build Image

```powershell
docker build -t my-router .
```

---

## 3. Clean Old Containers (IMPORTANT)

```powershell
docker rm -f router_a router_b router_c 2>$null
docker container prune -f
```

---

## 4. Start Router A (UPDATED IP)

```powershell
docker run -d --name router_a --privileged --network net_ab --ip 10.0.1.10 -e MY_IP=10.0.1.10 -e NEIGHBORS=10.0.1.11,10.0.3.12 -e LOCAL_SUBNETS=10.0.1.0/24,10.0.3.0/24 my-router

docker network connect net_ac router_a --ip 10.0.3.10
```

---

## 5. Start Router B (UPDATED IP)

```powershell
docker run -d --name router_b --privileged --network net_ab --ip 10.0.1.11 -e MY_IP=10.0.1.11 -e NEIGHBORS=10.0.1.10,10.0.2.12 -e LOCAL_SUBNETS=10.0.1.0/24,10.0.2.0/24 my-router

docker network connect net_bc router_b --ip 10.0.2.11
```

---

## 6. Start Router C (UPDATED IP)

```powershell
docker run -d --name router_c --privileged --network net_bc --ip 10.0.2.12 -e MY_IP=10.0.2.12 -e NEIGHBORS=10.0.2.11,10.0.3.10 -e LOCAL_SUBNETS=10.0.2.0/24,10.0.3.0/24 my-router

docker network connect net_ac router_c --ip 10.0.3.12
```

---

## 7. Useful Checks

```powershell
docker ps
docker logs router_a
docker exec -it router_a sh
```

Inside container:

```sh
ip route
```

---

## 8. Failure Test (Stop Router C)

```powershell
docker stop router_c
```

Wait **~20 seconds**, then check:

```powershell
docker exec -it router_a sh -c "ip route"
docker logs router_a
```

---

## 9. Expected Behavior

- Before failure:
  - Routes learned normally
- After stopping Router C:
  - Routes via C removed
  - Router A continues using Router B

---

## 10. Cleanup (Optional)

```powershell
docker rm -f router_a router_b router_c
docker network rm net_ab net_bc net_ac
```