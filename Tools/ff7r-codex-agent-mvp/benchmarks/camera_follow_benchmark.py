from dataclasses import dataclass
import math
from ff7r_agent.camera_follow import CameraFollowConfig, CameraFollowController

@dataclass(frozen=True)
class P:
    x: float
    y: float
    z: float = 0.0

ROUTES = {
    'straight': [P(0,0), P(2000,0)],
    'gentle_curve': [P(0,0), P(500,0), P(1000,150), P(1500,400), P(2000,700)],
    'turn_45': [P(0,0), P(600,0), P(1300,700)],
    'turn_90': [P(0,0), P(600,0), P(600,1200)],
    's_curve': [P(0,0), P(500,0), P(900,300), P(1300,0), P(1800,0)],
    'dense_straight': [P(i*100, (1 if i%2 else -1)*2) for i in range(21)],
}

def point_at(route, distance_cm):
    remaining=distance_cm
    for a,b in zip(route,route[1:]):
        dx,dy=b.x-a.x,b.y-a.y; length=math.hypot(dx,dy)
        if remaining <= length:
            t=0 if length==0 else remaining/length
            return P(a.x+t*dx,a.y+t*dy)
        remaining-=length
    return route[-1]

def simulate(route):
    cfg = CameraFollowConfig(camera_follow_enabled=True)
    ctl = CameraFollowController(cfg)
    camera = 0.0
    lengths=[math.hypot(b.x-a.x,b.y-a.y) for a,b in zip(route,route[1:])]
    total=sum(lengths)
    signs=[]; targets=[]; commands=[]
    for i in range(48):
        pos=point_at(route,total*min(0.98,i/47))
        out=ctl.command(route=route, position=pos, camera_yaw_deg=camera)
        cmd=float(out['camera_input_x'])
        camera += cmd*14.0
        targets.append(round(float(out.get('target_yaw_deg', camera)),2))
        commands.append(round(cmd,3))
        s=0 if abs(cmd)<1e-9 else (1 if cmd>0 else -1)
        if s: signs.append(s)
    reversals=sum(a!=b for a,b in zip(signs,signs[1:]))
    final=ctl.command(route=route, position=point_at(route,total*.98), camera_yaw_deg=camera)
    return targets,commands,reversals,max(abs(x) for x in commands),abs(float(final.get('yaw_error_deg',0)))<=cfg.camera_follow_deadband_deg

for name,route in ROUTES.items():
    target,commands,reversals,maximum,settled=simulate(route)
    print(f'{name}: reversals={reversals} max={maximum:.3f} settled={settled}')
    print('  target_yaw=',target)
    print('  command_x=',commands)
