import copy
import os
import tempfile
import time

from fedn.utils.helpers import KerasSequentialHelper

from .state import ReducerState


class Model:
    """ (DB) representation of a global model. """

    def __init__(self, id=None, model_type="Keras"):
        self.id = id
        self.name = ""
        self.type = model_type
        self.version = ""
        self.parent = ""
        self.alliance_uid = ""
        self.round_id = 0


class ReducerControl:

    def __init__(self, statestore):
        self.__state = ReducerState.idle
        self.statestore = statestore
        self.combiners = []

        # TODO remove temporary hardcoded config of storage persistance backend
        s3_config = {'storage_access_key': os.environ['FEDN_MINIO_ACCESS_KEY'],
                     'storage_secret_key': os.environ['FEDN_MINIO_SECRET_KEY'],
                     'storage_bucket': 'models',
                     'storage_secure_mode': False,
                     'storage_hostname': os.environ['FEDN_MINIO_HOST'],
                     'storage_port': int(os.environ['FEDN_MINIO_PORT'])}

        from fedn.common.storage.s3.s3repo import S3ModelRepository
        self.model_repository = S3ModelRepository(s3_config)
        self.bucket_name = s3_config["storage_bucket"]

        # TODO: Make configurable
        self.helper = KerasSequentialHelper()

    def get_latest_model(self):
        return self.statestore.get_latest()

    def get_model_info(self):
        return self.statestore.get_model_info()
     
    def commit(self, model_id, model=None):
        """ Commit a model. This establishes this model as the lastest consensus model. """

        if model:
            fod, outfile_name = tempfile.mkstemp(suffix='.h5')
            model.save(outfile_name)
            model_id = self.model_repository.set_model(outfile_name, is_file=True)
            os.unlink(outfile_name)

        self.statestore.set_latest(model_id)

    def _out_of_sync(self,combiners=None):

        if not combiners:
            combiners = self.combiners

        osync = []
        for combiner in combiners:
            model_id = combiner.get_model_id()
            if model_id != self.get_latest_model():
                osync.append(combiner)
        return osync

    def check_round_participation_policy(self,compute_plan,combiner_state):
        """ Evaluate reducer level policy for combiner round-paarticipation. 
            This is a decision on ReducerControl level, additional checks
            applies on combiner level. Not all reducer control flows might
            need or want to use a participation policy.  """
        if int(compute_plan['clients_required']) <= int(combiner_state['nr_active_clients']):
            return True
        else:
            return False

    def check_round_start_policy(self,combiners):
        """ Check if the overall network state meets a policy to start the round. """
        if len(combiners) > 0:
            return True
        else:
            return False

    def check_round_validity_policy(self,combiners):
        """ Before committing a model we check if a round validity policy has been met. """
        if len(combiners) > 0:
            return True
        else:
            return False 


    def round(self, config):
        """ Execute one global round. """

        # TODO: Set / update reducer states and such
        if len(self.combiners) < 1:
            print("REDUCER: No combiners connected!")
            return

        # 1. Formulate compute plans for this round and decide which combiners should participate in the round.
        compute_plan = copy.deepcopy(config)
        compute_plan['rounds'] = 1
        compute_plan['task'] = 'training'
        compute_plan['model_id'] = self.get_latest_model()

        combiners = []
        for combiner in self.combiners:
            combiner_state = combiner.report()
            is_participating = self.check_round_participation_policy(compute_plan,combiner_state)
            if is_participating:
                combiners.append((combiner,compute_plan))

        print("PARTICIPATING: {}".format(combiners),flush=True)

        round_start = self.check_round_start_policy(combiners)
        print("ROUND START POLICY: {}".format(round_start),flush=True)
        if not round_start:
            return None


        # 2. Sync up and ask participating combiners to coordinate model updates
        for combiner,compute_plan in combiners:        
            self.sync_combiners([combiner],self.get_latest_model())
            print(combiner,compute_plan,flush=True)
            response = combiner.start(compute_plan)

        # Wait until all participating combiners have a model that is out of sync with the current global model.
        # TODO: Implement strategies to handle timeouts. 
        # TODO: We do not need to wait until all combiners complete before we start reducing. 
        cl = []
        for combiner,plan in combiners:
            cl.append(combiner)

        wait = 0.0
        while len(self._out_of_sync(cl)) < len(combiners):
            time.sleep(1.0)
            wait += 1.0
            if wait >= config['round_timeout']:
                break

        # OBS! Here we are checking agains all combiners, not just those that computed in this round.
        # This means we let straggling combiners participate in the update 
        updated = self._out_of_sync()
        print("UPDATED: {}".format(updated),flush=True)


        round_valid = self.check_round_validity_policy(updated)
        if not round_valid:
            # TODO: Should we reset combiner state here? 
            return None

        # 3. Reduce combiner models into a global model
        # TODO, check success
        model = self.reduce(updated)
        
        if model:
            import uuid
            model_id = uuid.uuid4()
            self.commit(model_id,model)

            # 4. Trigger participating combiner nodes to execute a validation round for the current model
            combiner_config = copy.deepcopy(config)
            combiner_config['model_id'] = self.get_latest_model()
            combiner_config['task'] = 'validation'
            for combiner in updated:
                combiner.start(combiner_config)
            return model_id
        else:
            print("REDUCER: failed to updated model in round with config {}".format(config),flush=True)
            return None

    def sync_combiners(self, combiners, model_id):
        """ Spread the current consensus model to all active combiner nodes. """
        if not model_id:
            print("GOT NO MODEL TO SET! Have you seeded the FedML model?", flush=True)
            return

        for combiner in combiners:
            response = combiner.set_model_id(model_id)

    def instruct(self, config):
        """ Main entrypoint, executes the compute plan. """

        if self.__state == ReducerState.instructing:
            print("Already set in INSTRUCTING state", flush=True)
            return

        self.__state = ReducerState.instructing

        if not self.get_latest_model():
            print("No model in model chain, please seed the alliance!")

        self.__state = ReducerState.monitoring

        for round in range(int(config['rounds'])):
            model_id = self.round(config)
            if model_id:
                print("REDUCER: Global round completed, new model: {}".format(model_id),flush=True)
            else:
                print("REDUCER: Global round failed!")


        self.__state = ReducerState.idle

    def reduce(self, combiners):
        """ Combine current models at Combiner nodes into one global model. """
        i = 1
        for combiner in combiners:
            data = combiner.get_model()
            if data:
                try:
                    model_next = self.helper.load_model(combiner.get_model().getbuffer())
                    self.helper.increment_average(model, model_next, i)
                except:
                    model = self.helper.load_model(data.getbuffer())
                i = i+1
        return model

    def reduce_random(self, combiners):
        """ This is only used for debugging purposes. s"""
        import random
        combiner = random.sample(combiners, 1)[0]
        import uuid
        model_id = uuid.uuid4()
        return self.helper.load_model(combiner.get_model().getbuffer()),model_id

    def resolve(self):
        """ At the end of resolve, all combiners have the same model state. """

        combiners = self._out_of_sync()
        if len(combiners) > 0:
            model = self.reduce(combiners)
        return model

    def monitor(self, config=None):
        """ monitor """
        #if self.__state == ReducerState.monitoring:
            #print("monitoring")

    def add(self, combiner):
        if self.__state != ReducerState.idle:
            print("Reducer is not idle, cannot add additional combiner")
            return
        if self.find(combiner.name):
            return
        print("adding combiner {}".format(combiner.name), flush=True)
        self.combiners.append(combiner)

    def remove(self, combiner):
        if self.__state != ReducerState.idle:
            print("Reducer is not idle, cannot remove combiner")
            return
        self.combiners.remove(combiner)

    def find(self, name):
        for combiner in self.combiners:
            if name == combiner.name:
                return combiner
        return None

    def find_available_combiner(self):
        # TODO: Extend with more types of client allocation schemes. 
        for combiner in self.combiners:
            if combiner.allowing_clients():
                return combiner
        return None

    def state(self):
        return self.__state
