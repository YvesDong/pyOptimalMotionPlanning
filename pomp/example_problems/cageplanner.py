from OpenGL.GL import *
from .geometric import *
from ..spaces.objective import *
from ..spaces.statespace import *
from ..spaces.configurationspace import *
from ..spaces.edgechecker import *
from ..spaces.metric import *
from ..planners.problem import PlanningProblem
from ..bullet.forward_simulator import *
import math

# TODO: 1. Discontinuity of movement in Pybullet replay
# 2. Cage-based robustness penalty term in the objective function
# 3. Continuous visualization in OpenGL

class CagePlannerControlSpace(ControlSpace):
    def __init__(self,cage):
        self.cage = cage
        self.dynamics_sim = forward_simulation(cage.params)
        self.cagePlanner = True
        self.half_extents_gripper = cage.half_extents_gripper # [x,z]
    def configurationSpace(self):
        return self.cage.configurationSpace()
    def controlSet(self,x):
        return MultiSet(TimeBiasSet(self.cage.time_range,self.cage.controlSet()),self.cage.controlSet())
    def nextState(self,x,u):
        return self.eval(x,u,1.0)
    def toBulletStateInput(self, x, u):
        # OpenGL (O) Cartesian coordiantes are different from Bullet (B)
        # O--->---------
        # |             | 
        # \/     *      | 
        # |    =====    | 
        # /\            | 
        #  |            | 
        # B--->---------
        q = [x[0],self.cage.y_range-x[1],
             x[2],-x[3],
             x[4],self.cage.y_range-x[5],-x[6],
             x[7],-x[8],-x[9]]
        mu = [u[0],u[1],-u[2],-u[3]]
        return q, mu
    
    def toOpenglStateInput(self, q):
        x = [q[0],self.cage.y_range-q[1],
             q[2],-q[3],
             q[4],self.cage.y_range-q[5],-q[6],
             q[7],-q[8],-q[9]]
        return x
         
    def eval(self,x,u,amount):
        # xo,yo,vox,voy,xg,yg,thetag,vgx,vgy,omegag = x # state space, 10D (4: cage, 6: gripper)
        t,thrust_x,thrust_y,alpha = u # control space, 4D
        tc = t*amount
        u = [tc,thrust_x,thrust_y,alpha]
        q, mu = self.toBulletStateInput(x, u)
        self.dynamics_sim.reset_states(q)
        q_new = self.dynamics_sim.run_forward_sim(mu)
        x_new = self.toOpenglStateInput(q_new)

        return x_new
    
    def interpolator(self,x,u):
        return LambdaInterpolator(lambda s:self.eval(x,u,s),self.configurationSpace(),10)

class CagePlanner:
    def __init__(self):
        self.x_range = 10
        self.y_range = 10
        self.max_velocity = 10
        self.max_acceleration = 10

        # Parameters passing to Pybullet
        self.mass_object = 1
        self.mass_gripper = 10
        self.moment_gripper = 1 # moment of inertia
        self.half_extents_gripper = [.5, .1] # movement on x-z plane
        self.radius_object = 0.01
        self.params = [self.mass_object, self.mass_gripper, self.moment_gripper, 
                       self.half_extents_gripper, self.radius_object]
        
        yo_init = 4
        yo_goal = 8
        self.start_state = [2,yo_init,0,0,2,yo_init+self.radius_object+self.half_extents_gripper[1],0,0,0,0]
        self.goal_state = [8,yo_goal,0,0,0,0,0,0,0,0]
        self.goal_radius = .5
        self.time_range = 1

        self.obstacles = []
        self.gravity = 9.81 # downward in openGL vis

    def controlSet(self):
        return BoxSet([-self.max_acceleration, -self.gravity-self.max_acceleration, -.1], 
                      [self.max_acceleration, -self.gravity+self.max_acceleration/10, .1])

    def controlSpace(self):
        # System dynamics
        return CagePlannerControlSpace(self)

    def workspace(self):
        # For visualization
        wspace = Geometric2DCSpace()
        wspace.box.bmin = [0,0]
        wspace.box.bmax = [self.x_range,self.y_range]
        wspace.addObstacleParam(self.obstacles)
        for o in self.obstacles:
            wspace.addObstacle(Box(o[0],o[1],o[0]+o[2],o[1]+o[3]))
        return wspace
    
    def configurationSpace(self):
        wspace = Geometric2DCSpace()
        wspace.box.bmin = [0,0]
        wspace.box.bmax = [self.x_range,self.y_range]
        wspace.addObstacleParam(self.obstacles)
        for o in self.obstacles:
            wspace.addObstacle(Box(o[0],o[1],o[0]+o[2],o[1]+o[3]))
        res =  MultiConfigurationSpace(wspace,
                                       BoxConfigurationSpace([-self.max_velocity],[self.max_velocity]), 
                                       BoxConfigurationSpace([-self.max_velocity],[self.max_velocity]),
                                       BoxConfigurationSpace([0],[self.x_range]),
                                       BoxConfigurationSpace([0],[self.y_range]),
                                       BoxConfigurationSpace([-math.pi],[math.pi]), 
                                       BoxConfigurationSpace([-self.max_velocity],[self.max_velocity]), 
                                       BoxConfigurationSpace([-self.max_velocity],[self.max_velocity]),
                                       BoxConfigurationSpace([-self.max_velocity],[self.max_velocity]),
                                       )
        return res

    def startState(self):
        return self.start_state

    def goalSet(self):
        r = self.goal_radius
        return BoxSet([self.goal_state[0]-r, self.goal_state[1]-r,
                       -self.max_velocity, -self.max_velocity, 
                       0.0, 0.0, -math.pi,
                       -self.max_velocity, -self.max_velocity, -self.max_velocity],
                      [self.goal_state[0]+r, self.goal_state[1]+r,
                       self.max_velocity, self.max_velocity, 
                       self.x_range, self.y_range, math.pi,
                       self.max_velocity, self.max_velocity, self.max_velocity])


class CagePlannerObjectiveFunction(ObjectiveFunction):
    """Given a function pointwise(x,u), produces the incremental cost
    by incrementing over the interpolator's length.
    """
    def __init__(self,cage,timestep=0.2):
        self.cage = cage
        self.space = cage.controlSpace()
        self.timestep = timestep
        self.masso = cage.params[0]
        self.massg = cage.params[1]
        self.momentg = cage.params[2]
    def incremental(self,x,u):
        xnext = self.space.nextState(x,u)
        g = self.cage.gravity
        
        # Energy E_k+E_g total increase cost (BUG: root node is asked to be pruned without max)
        # E = -g*(self.cage.y_range-x[1]) + 0.5*(x[2]**2+x[3]**2)
        # Enext = -g*(self.cage.y_range-xnext[1]) + 0.5*(xnext[2]**2+xnext[3]**2)
        # c = max((Enext-E), 0.0)

        # Distance from goal region
        xo_goal = self.cage.goal_state[:2]
        xo = x[:2]
        xo_next = xnext[:2]
        dis = math.sqrt(sum([(xo_goal[i]-xo[i])**2 for i in range(len(xo))]))
        dis_next = math.sqrt(sum([(xo_goal[i]-xo_next[i])**2 for i in range(len(xo))]))
        c1 = max(dis_next-dis, 0.01)

        # Object and gripper total energy (kinetic and potential)
        E_o = self.masso * (g*(self.cage.y_range-x[1]) + 0.5*(x[2]**2+x[3]**2))
        Enext_o = self.masso * (g*(self.cage.y_range-xnext[1]) + 0.5*(xnext[2]**2+xnext[3]**2))
        E_g = g*self.massg*(self.cage.y_range-x[5]) + 0.5*(self.massg*(x[7]**2+x[8]**2)+self.momentg*(x[9]**2))
        Enext_g = g*self.massg*(self.cage.y_range-xnext[5]) + 0.5*(self.massg*(xnext[7]**2+xnext[8]**2)+self.momentg*(x[9]**2))
        c2 = max((Enext_g+Enext_o-E_o-E_g), 0.0)

        # Time is penalized
        return c1 + 0.001*u[0]
        # return 10*c1 + 0.001*c2 + u[0]


def CagePlannerTest():
    p = CagePlanner()
    objective = CagePlannerObjectiveFunction(p)
    return PlanningProblem(p.controlSpace(),p.startState(),p.goalSet(),
                           objective=objective,
                           visualizer=p.workspace(),
                           euclidean = True)


