import copy
import json
import random
from typing import Callable, Dict, List, Optional, Tuple, Union
import gym
from gym import spaces
import pickle
import numpy as np
import configparser
from pathlib import Path
from UserTools.createMolecule import getMolecule

from pysisyphus.Geometry import Geometry
from pysisyphus.optimizers.MyOptimizer import MyOptimizer
from pysisyphus.calculators.RDKit import MMFF
from pysisyphus.constants import ANG2BOHR, BOHR2ANG
from pysisyphus.drivers.opt import get_opt_cls
from lib.ModelType import TrainingData


from functions.storeAction import StoreActionMethod
from functions.getState import State
from functions.getAction import Action
from functions.HistoryState import HistoryState
from functions.Factory import reward_function_dict, \
    STATE_CLASS_DICT,\
    ACTION_CLASS_DICT,\
    STORE_ACTION_CLASS_DICT
import time
import warnings
warnings.filterwarnings("error", category=RuntimeWarning)
# warnings.filterwarnings("error::RuntimeWarning")

class MoleculeV20(gym.Env):
    '''
    many kinds of molecules
    '''
    def __init__(self,
                configs: configparser.ConfigParser
                 ) -> None:
        super().__init__()
        with open("envs/perfect_primitives.pk", "rb")as fs:
            self.default_primitives: dict = pickle.load(fs)
        # with open("envs/smiles_1243_991_972.json", "r")as fs:
        #     self.smiles: List[str] = json.load(fs)

        with open("envs/smiles_258.json", "r")as fs:
            self.smiles: List[str] = json.load(fs)
            # smiles_259
        
        self.env_iteration: int = 0

        smile = "C"
        _smile, atoms, coords, _seed = getMolecule(smile, seed=1)
        
        geometry = Geometry(atoms=atoms, coords=coords*ANG2BOHR, coord_type="redund", coord_kwargs={
            "typed_prims": self.default_primitives.get(smile),
        })
        geometry.set_calculator(MMFF(smile=smile, seed=_seed))

        self.set_params(configs)
        self.Action: Action = self.action_class(geometry)
        self.State: State = self.state_class(geometry)
        self.StoreActionMethod: StoreActionMethod = self.store_action_class(
            geometry)

        print("using State class:", self.State.__class__.__name__)
        print("using Action class:", self.Action.__class__.__name__)
        print("using StoreActionMethod class:",
              self.StoreActionMethod.__class__.__name__)

        self.history_state = HistoryState(
            n_size=self.n_history,
            state=self.State,
            action=self.Action,
            store_action=self.StoreActionMethod,
            max_coords_length=len(geometry.coords),
        )

        low, high = self.Action.getActionSpace()

        self.action_space = spaces.Box(
            low=np.array(low),
            high=np.array(high),
            shape=np.array(low).shape,
            dtype=np.float32
        )

        self.observation_space = spaces.Box(
            low=-np.inf,
            high=np.inf,
            shape=(self.history_state.state_size, ),
            dtype=np.float32
        )

    def set_params(self,
                configs: configparser.ConfigParser
                   ):
        self.max_force: float = configs.getfloat("max_force")
        self.max_step: float = configs.getfloat("max_step")
        self.reward_function: Callable = reward_function_dict[configs.get("reward_index")]
        self.state_class = STATE_CLASS_DICT[configs.get("state_index")]
        self.action_class = ACTION_CLASS_DICT[configs.get("action_index")]
        self.store_action_class = STORE_ACTION_CLASS_DICT[configs.get("store_action_index")]
        self.n_history: int = configs.getint("n_history")
        self.max_iter: int = configs.getint("max_iter")
        # self.max_coord_length: int = configs.getint("max_coord_length")
        self.opt_key: str = configs.get("opt_key")

        self.expert_participation: float = configs.getfloat("expert_participation")
        assert 1 >= self.expert_participation >= 0, "expert_participation is not in range 0~1"

        self.isLimitMaxStep: bool = configs.getboolean("isLimitMaxStep")
        print("Limit Max Step", self.isLimitMaxStep)
    
    def createGeometry(self, smile: str, coord: np.ndarray) -> Tuple[List[str], Geometry]:
        _smile, atoms, _coords, _seed = getMolecule(smile, random.randint(1, 2**30))
        if type(coord) == np.ndarray:
            coords = coord
        
        geometry = Geometry(atoms=atoms, coords=coords*ANG2BOHR, coord_type="redund", coord_kwargs={
            "typed_prims": self.default_primitives.get(smile),
        })
        geometry.set_calculator(MMFF(smile=smile, seed=_seed))
        return atoms, geometry

    def step(self, action: np.ndarray):
        self.env_iteration += 1
        error = False
        # print("raw_action:", action)
        if np.sum(np.isnan(action)) != 0:
            print("prvious state", self.history_state.getState())
            raise Exception("action nan")
        
        try:
            rl_action = self.Action.getAction(action, self.geometry.gradient)
            # print("rl_action:", rl_action)
            if np.sum(np.isnan(rl_action)) != 0:
                print("raw action", action)
                print("prvious state", self.history_state.getState())
                raise Exception("rl_action nan")
            (cur_cycle, optimizer_action) = next(
                self.iterator)         # take a step
            
            if isinstance(self.optimizer, MyOptimizer):
                pass
            else:
                rl_action = (1-self.expert_participation)*rl_action + self.expert_participation * optimizer_action

            if self.isLimitMaxStep is True:
                rl_action = self.optimizer.scale_by_max_step(copy.deepcopy(rl_action)) - optimizer_action
            is_converged = self.iterator.send(rl_action)                # after the step


            reward = self.reward_function(self.optimizer.max_forces[-1])

            self.iter += 1
            is_converged = next(self.iterator)
            self.history_state.append(self.State.getState(), rl_action)
            self.state = self.history_state.getState()
            self.max_forces.append(self.geometry.forces.max())

            if self.optimizer.max_forces[-1] > 100:
                raise Exception

            if self.optimizer.max_forces[-1] < self.max_force:
                print("converged")
                raise StopIteration

            if is_converged or self.iter >= self.max_iter:
                # next(self.iterator)
                raise StopIteration


        except StopIteration as result:
            # End time
            self.end_time = time.time()
            print("End Smile:", self.smile, ", result:", result)
            print("stop StopIteration", self.iter)
            print("using time(s)", round(self.end_time - self.start_time, 5))
            reward = +5 if self.iter < self.max_iter else 0
            is_converged = True
            error = False
        except (ValueError, RuntimeWarning) as e:
            print("haha", e)
            reward = -self.max_iter+1
            is_converged = True
            error = True

        except Exception as e:
            # bfgs reset() is not defined
            # print(f"action {action.shape}:", action)
            # print(f"rl_action {rl_action.shape}:", rl_action)
            # print(f"geometry {len(self.geometry.coords)}:",
            #       self.geometry.coords)
            print("Exception type:", e.__class__.__name__)
            print(e.__traceback__)
            print('-------------Attr:', e)
            trace = []
            tb = e.__traceback__
            while tb is not None:
                trace.append({
                    "filename": tb.tb_frame.f_code.co_filename,
                    "name": tb.tb_frame.f_code.co_name,
                    "lineno": tb.tb_lineno
                })
                tb = tb.tb_next
            print(str({
                'type': type(e).__name__,
                'message': str(e),
                'trace': trace
            }))
            # raise Exception()
            reward = -self.max_iter+1
            is_converged = True
            error = True


        finally:
            state = self.state
        return TrainingData(
            atoms=self.geometry.atoms,
            coords=self.geometry.cart_coords*BOHR2ANG,
            pre_features=self.history_state.getState(),
        ), reward, bool(is_converged), {
            "error": error,
            "rebuild": 0,
        }

    def reset(self, smile: str=None, coord=None):
        self.iter = 0
        
        noise_probability = 0.5
        self.max_forces: List[float] = []

        if smile != None:
            self.smile = smile
        else:
            self.smile = random.choice(self.smiles)
        print("using smile:", self.smile)
        
       # initial geoemtry force needs larger then threshold
        atoms, tmp_geometry = self.createGeometry(self.smile, coord)

        # Start time
        self.start_time = time.time()
        
        self.geometry = tmp_geometry
        self.Action: Action = self.action_class(self.geometry)
        self.State: State = self.state_class(self.geometry)
        self.StoreActionMethod: StoreActionMethod = self.store_action_class(
            self.geometry)
        
        self.max_coord_length: int = len(self.geometry.coords)


        # self.history_state.reset()
        self.history_state = HistoryState(
            n_size=self.n_history,
            state=self.State,
            action=self.Action,
            store_action=self.StoreActionMethod,
            max_coords_length=self.max_coord_length,
        )
        if self.expert_participation > 0:
            self.optimizer = get_opt_cls(self.opt_key)(self.geometry,
                                    max_cycles=self.max_iter-1,
                                    thresh="gau",
                                    # thresh="gau_loose",
                                    max_force_only=True,
                                    max_step=self.max_step,
                                )
        else:
            self.optimizer = MyOptimizer(self.geometry,
                                        max_cycles=self.max_iter-1,
                                        thresh="gau",
                                        # thresh="gau_loose",
                                        max_force_only=True,
                                        max_step=self.max_step,
                                        )
        print("using Optimizer:", self.optimizer.__class__.__name__)
        self.iterator = self.optimizer.run()
        self.history_state.first_append(self.State.getState())
        self.env_iteration += 1
        return  TrainingData(
            atoms=self.geometry.atoms,
            coords=self.geometry.cart_coords*BOHR2ANG,
            pre_features=self.history_state.getState(),
        )
