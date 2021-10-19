
import torch
from tqdm import tqdm
import pandas as pd
import numpy as np
from copy import deepcopy
import torch
from gnn_reco.data.sqlite_dataset import SQLiteDataset
from sklearn.model_selection import train_test_split
from torch_geometric.data.batch import Batch
import os
from sklearn.preprocessing import RobustScaler
import sqlite3
import pandas as pd
import pickle
import numpy as np
import os

class EarlyStopping(object):
    def __init__(self, mode='min', min_delta=0, patience=10, percentage=False):
        self.mode = mode
        self.min_delta = min_delta
        self.patience = patience
        self.best = None
        self.num_bad_epochs = 0
        self.is_better = None
        self._init_is_better(mode, min_delta, percentage)

        if patience == 0:
            self.is_better = lambda a, b: True
            self.step = lambda a: False

    def step(self, metrics,model):
        if self.best is None:
            self.best = metrics
            return False

        if torch.isnan(metrics):
            return True

        if self.is_better(metrics, self.best):
            self.num_bad_epochs = 0
            self.best = metrics
            self.best_params = model.state_dict()
        else:
            self.num_bad_epochs += 1

        if self.num_bad_epochs >= self.patience:
            return True

        return False

    def _init_is_better(self, mode, min_delta, percentage):
        if mode not in {'min', 'max'}:
            raise ValueError('mode ' + mode + ' is unknown!')
        if not percentage:
            if mode == 'min':
                self.is_better = lambda a, best: a < best - min_delta
            if mode == 'max':
                self.is_better = lambda a, best: a > best + min_delta
        else:
            if mode == 'min':
                self.is_better = lambda a, best: a < best - (
                            best * min_delta / 100)
            if mode == 'max':
                self.is_better = lambda a, best: a > best + (
                            best * min_delta / 100)
    def GetBestParams(self):
        return self.best_params

class PiecewiseLinearScheduler(object):
    def __init__(self, training_dataset, start_lr, max_lr, end_lr, max_epochs):
        self._start_lr = start_lr
        self._max_lr   = max_lr
        self._end_lr   = end_lr
        self._steps_up = int(len(training_dataset)/2)
        self._steps_down = len(training_dataset)*max_epochs - self._steps_up
        self._current_step = 0
        self._lr_list = self._calculate_lr_list()

    def _calculate_lr_list(self):
        res = list()
        for step in range(0,self._steps_up+self._steps_down):
            slope_up = (self._max_lr - self._start_lr)/self._steps_up
            slope_down = (self._end_lr - self._max_lr)/self._steps_down
            if step <= self._steps_up:
                res.append(step*slope_up + self._start_lr)
            if step > self._steps_up:
                res.append(step*slope_down + self._max_lr -((self._end_lr - self._max_lr)/self._steps_down)*self._steps_up)
        return torch.tensor(res)

    def get_next_lr(self):
        lr = self._lr_list[self._current_step]
        self._current_step = self._current_step + 1
        return lr

class MultipleDatabasesTrainer(object):
    def __init__(self, databases, selections, pulsemap, batch_size, FEATURES, TRUTH, num_workers,optimizer, n_epochs, loss_func, target, device, scheduler = None, patience = 10, early_stopping = True):
        self.databases = databases
        self.selections = selections
        self.pulsemap = pulsemap
        self.batch_size  = batch_size
        self.FEATURES = FEATURES
        self.TRUTH = TRUTH
        self.num_workers = num_workers
        if early_stopping:
            self._early_stopping_method = EarlyStopping(patience = patience)
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.n_epochs = n_epochs
        self.loss_func = loss_func
        self.target = target
        self.device = device

        self._setup_dataloaders()

    def __call__(self, model):
        trained_model = self._train(model)
        self._load_best_parameters(model)
        return trained_model
    
    def _setup_dataloaders(self):
        self.training_dataloaders = []
        self.validation_dataloaders = []
        for i in range(len(self.databases)):
            db = self.databases[i]
            selection = self.selections[i]
            training_dataloader, validation_dataloader = make_train_validation_dataloader(db, selection, self.pulsemap, self.batch_size, self.FEATURES, self.TRUTH, self.num_workers)
            self.training_dataloader.append(training_dataloader)
            self.validation_dataloader.append(validation_dataloader)
        return
    def _count_minibatches(self):
        training_batches = 0
        for i in range(len(self.training_dataloaders)):
            training_batches +=len(self.training_dataloaders[i])
        return training_batches

    def _train(self,model):
        training_batches = self._count_minibatches()
        for epoch in range(self.n_epochs):
            acc_loss = torch.tensor([0],dtype = float).to(self.device)
            iteration = 1
            model.train()
            pbar = tqdm(total = training_batches, unit= 'batches') 
            for training_dataloader in self.training_dataloaders:  
                for batch_of_graphs in training_dataloader:
                    batch_of_graphs.to(self.device)
                    with torch.enable_grad():                                                                                                     
                            self.optimizer.zero_grad()                                                   
                            out                             = model(batch_of_graphs)   
                            loss                            = self.loss_func(out, batch_of_graphs, self.target)                      
                            loss.backward()                                                         
                            self.optimizer.step()
                    if self.scheduler != None:    
                        self.optimizer.param_groups[0]['lr'] = self.scheduler.get_next_lr().item()
                    acc_loss += loss
                    iteration +=1
                    pbar.update(iteration)
                    pbar.set_description('epoch: %s || loss: %s'%(epoch, acc_loss.item()/iteration))
            validation_loss = self._validate(model)
            pbar.set_description('epoch: %s || loss: %s || valid loss : %s'%(epoch,acc_loss.item()/iteration, validation_loss.item()))
            if self._early_stopping_method.step(validation_loss,model):
                print('EARLY STOPPING: %s'%epoch)
                break
        return model
    def _validate(self,model):
        acc_loss = torch.tensor([0],dtype = float).to(self.device)
        model.eval()
        iterations = 1
        for validation_dataloader in self.validation_dataloaders:
            for batch_of_graphs in validation_dataloader:
                batch_of_graphs.to(self.device)
                with torch.no_grad():                                                                                        
                    out                             = model(batch_of_graphs)   
                    loss                            = self.loss_func(out, batch_of_graphs, self.target)                             
                    acc_loss += loss
                iterations +=1
        return acc_loss/iterations
    
    def _load_best_parameters(self,model):
        return model.load_state_dict(self._early_stopping_method.GetBestParams())

class Trainer(object):
    def __init__(self, training_dataloader, validation_dataloader, optimizer, n_epochs, loss_func, target, device, scheduler = None, patience = 10, early_stopping = True):
        self.training_dataloader = training_dataloader
        self.validation_dataloader = validation_dataloader
        if early_stopping:
            self._early_stopping_method = EarlyStopping(patience = patience)
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.n_epochs = n_epochs
        self.loss_func = loss_func
        self.target = target
        self.device = device

    def __call__(self, model):
        trained_model = self._train(model)
        self._load_best_parameters(model)
        return trained_model

    def _train(self,model):
        for epoch in range(self.n_epochs):
            acc_loss = torch.tensor([0],dtype = float).to(self.device)
            iteration = 1
            model.train() 
            pbar = tqdm(self.training_dataloader, unit= 'batches')
            for batch_of_graphs in pbar:
                batch_of_graphs.to(self.device)
                with torch.enable_grad():                                                                                                     
                        self.optimizer.zero_grad()                                                   
                        out                             = model(batch_of_graphs)   
                        loss                            = self.loss_func(out, batch_of_graphs, self.target)                      
                        loss.backward()                                                         
                        self.optimizer.step()
                if self.scheduler != None:    
                    self.optimizer.param_groups[0]['lr'] = self.scheduler.get_next_lr().item()
                acc_loss += loss
                if iteration == (len(pbar)):    
                    validation_loss = self._validate(model)
                    pbar.set_description('epoch: %s || loss: %s || valid loss : %s'%(epoch,acc_loss.item()/iteration, validation_loss.item()))
                else:
                    pbar.set_description('epoch: %s || loss: %s'%(epoch, acc_loss.item()/iteration))
                iteration +=1
            if self._early_stopping_method.step(validation_loss,model):
                print('EARLY STOPPING: %s'%epoch)
                break
        return model
            
    def _validate(self,model):
        acc_loss = torch.tensor([0],dtype = float).to(self.device)
        model.eval()
        for batch_of_graphs in self.validation_dataloader:
            batch_of_graphs.to(self.device)
            with torch.no_grad():                                                                                        
                out                             = model(batch_of_graphs)   
                loss                            = self.loss_func(out, batch_of_graphs, self.target)                             
                acc_loss += loss
        return acc_loss/len(self.validation_dataloader)
    
    def _load_best_parameters(self,model):
        return model.load_state_dict(self._early_stopping_method.GetBestParams())
         

class Predictor(object):
    def __init__(self, dataloader, target, device, output_column_names, post_processing_method = None):
        self.dataloader = dataloader
        self.target = target
        self.output_column_names = output_column_names
        self.device = device
        self.post_processing_method = post_processing_method
    def __call__(self, model):
        self.model = model
        self.model.eval()
        self.model.predict = True
        if self.post_processing_method == None:
            return self._predict()
        else:
            return self.post_processing_method(self._predict(),self.target)

    def _predict(self):
        predictions = []
        event_nos   = []
        target      = []
        with torch.no_grad():
            for batch_of_graphs in tqdm(self.dataloader, unit = 'batches'):
                batch_of_graphs.to(self.device)
                target.extend(batch_of_graphs[self.target].detach().cpu().numpy())
                predictions.extend(self.model(batch_of_graphs).detach().cpu().numpy())
                event_nos.extend(batch_of_graphs['event_no'].detach().cpu().numpy())
        out = pd.DataFrame(data = predictions, columns = self.output_column_names)
        out['event_no'] = event_nos
        out[self.target] = target
        return out
     

def make_train_validation_dataloader(db, selection, pulsemap, batch_size, FEATURES, TRUTH, num_workers):
    training_selection, validation_selection = train_test_split(selection, test_size=0.33, random_state=42)

    training_dataset = SQLiteDataset(db, pulsemap, FEATURES, TRUTH, selection= training_selection)
    training_dataset.close_connection()
    training_dataloader = torch.utils.data.DataLoader(training_dataset, batch_size=batch_size, shuffle=True, num_workers=num_workers, 
                                            collate_fn=Batch.from_data_list,persistent_workers=True,prefetch_factor=2)

    validation_dataset = SQLiteDataset(db, pulsemap, FEATURES, TRUTH, selection= validation_selection)
    validation_dataset.close_connection()
    validation_dataloader = torch.utils.data.DataLoader(validation_dataset, batch_size=batch_size, shuffle=True, num_workers=num_workers, 
                                            collate_fn=Batch.from_data_list,persistent_workers=True,prefetch_factor=2)
    return training_dataloader, validation_dataloader

def save_results(db, tag, results, archive,model):
    db_name = db.split('/')[-1].split('.')[0]
    path = archive + '/' + db_name + '/' + tag
    os.makedirs(path, exist_ok = True)
    results.to_csv(path + '/results.csv')
    torch.save(model.cpu(), path + '/' + tag + '.pkl')
    print('Results saved at: \n %s'%path)
    return

def check_db_size(db):
    max_size = 5000000
    with sqlite3.connect(db) as con:
        query = 'select event_no from truth'
        events =  pd.read_sql(query,con)
    if len(events) > max_size:
        events = events.sample(max_size)
    return events        

def fit_scaler(db, features,truth, pulsemap):
    features = deepcopy(features)
    truth = deepcopy(truth)
    features.remove('event_no')
    truth.remove('event_no')
    truth =  ', '.join(truth)
    features = ', '.join(features)

    outdir = '/'.join(db.split('/')[:-2]) 
    print(os.path.exists(outdir + '/meta/transformers.pkl'))
    if os.path.exists(outdir + '/meta/transformers.pkl'):
        comb_scalers = pd.read_pickle(outdir + '/meta/transformers.pkl')
    # else:
    #     truths = ['energy', 'position_x', 'position_y', 'position_z', 'azimuth', 'zenith']
    #     events = check_db_size(db)
    #     print('Fitting to %s'%pulsemap)
    #     with sqlite3.connect(db) as con:
    #         query = 'select %s from %s where event_no in %s'%(features,pulsemap, str(tuple(events['event_no'])))
    #         feature_data = pd.read_sql(query,con)
    #         scaler = RobustScaler()
    #         feature_scaler= scaler.fit(feature_data)
    #     truth_scalers = {}
    #     for truth in truths:
    #         print('Fitting to %s'%truth)
    #         with sqlite3.connect(db) as con:
    #             query = 'select %s from truth'%truth
    #             truth_data = pd.read_sql(query,con)
    #         scaler = RobustScaler()
    #         if truth == 'energy':
    #             truth_scalers[truth] = scaler.fit(np.array(np.log10(truth_data[truth])).reshape(-1,1))
    #         else:
    #             truth_scalers[truth] = scaler.fit(np.array(truth_data[truth]).reshape(-1,1))

    #     comb_scalers = {'truth': truth_scalers, 'input': feature_scaler}
    #     os.makedirs(outdir + '/meta', exist_ok= True)
    #     with open(outdir + '/meta/transformersv2.pkl','wb') as handle:
    #         pickle.dump(comb_scalers,handle,protocol = pickle.HIGHEST_PROTOCOL)
    return comb_scalers
