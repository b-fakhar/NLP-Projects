# -*- coding: utf-8 -*-
"""
Script for dataloader, training, evaluating, and saving finetuned, multi-label classification model based on pretrained
BERT parameters.

Programmed by Behnaaz Fakhar <fakhar.behnaz@gmail.com>
*    2023-02-21 Initial coding
"""


import torch, os


import torch.nn                 as nn
import numpy                    as np
import pandas                   as pd
import matplotlib.pyplot        as plt


from tqdm                       import tqdm
from sklearn.model_selection    import train_test_split
from torch.utils.data           import Dataset, DataLoader
from torch.nn.functional        import cross_entropy
from datasets                   import DatasetDict
from datasets                   import Dataset as HF_Dataset
from datasets                   import load_from_disk
from datasets                   import load_metric
from transformers               import AutoTokenizer, AutoModel, AutoModelForSequenceClassification, AutoConfig
from transformers               import get_scheduler
from sklearn.metrics            import roc_auc_score, f1_score, hamming_loss,accuracy_score

def df_to_DatasetDict(df, 
                    train_size, 
                    val_size, 
                    test_size=None, 
                    dataset_dir=None,
                    frac=1, 
                    random_state=42):
    
    # Determine the length of train, validation, and test sets
    train_len = int(len(df) * train_size)
    val_len   = int(len(df) * val_size)

    
    # Shuffle the DataFrame
    shuffled_df = df.sample(frac=frac, random_state=random_state)  # frac=1 shuffles all rows, random_state=42 for reproducibility
    
    # Split the DataFrame into train, validation, and test sets
    train_df = shuffled_df[:train_len]
    val_df = shuffled_df[train_len:train_len + val_len]

    
    # Convert DataFrames to Datasets
    train_dataset = HF_Dataset.from_pandas(train_df)
    val_dataset = HF_Dataset.from_pandas(val_df)

    
    # Rename the '__index_level_0__' column to 'idx' in each split of the dataset
    train_dataset = train_dataset.rename_column('__index_level_0__', 'idx')
    val_dataset = val_dataset.rename_column('__index_level_0__', 'idx')
    
    # Organize Datasets into a dictionary
    data_dict = {'train': train_dataset, 
                 'validation': val_dataset}

    
    if test_size:
        test_len = int(len(df) * test_size)
        test_df = shuffled_df[train_len + val_len:]
        test_dataset = HF_Dataset.from_pandas(test_df)
        test_dataset = test_dataset.rename_column('__index_level_0__', 'idx')
        # Add the test dataset to data_dict
        data_dict['test'] = test_dataset
    

    # Convert to DatasetDict
    data_dict = DatasetDict(data_dict)
    
    if dataset_dir:
        data_dict.save_to_disk(os.path.join(dataset_dir))
    else:
        return data_dict

class MyDataset (Dataset):
    def __init__(self, dataset_dir, tokenizer, max_len = 128):
        self.dataset_dir = dataset_dir
        self.tokenizer = tokenizer
        self.max_len = max_len
        
        self.dataset = load_from_disk(dataset_dir)
        self.texts = self.dataset['text']
        self.labels = self.dataset['labels']
        
    
    def __len__(self):
        return len(self.texts)
    
    def __getitem__(self, idx):
        text = str(self.texts[idx])
        label = torch.tensor(self.labels[idx])
        
        encoding = self.tokenizer.encode_plus(text,
                                              add_special_tokens    = True,
                                              max_length            = self.max_len,
                                              return_token_type_ids = True,
                                              padding               = "max_length",
                                              truncation            = True,
                                              return_attention_mask = True,
                                              return_tensors='pt')
        
        return {'input_ids'     : encoding['input_ids'].squeeze(0),
                'attention_mask': encoding['attention_mask'].squeeze(0),
                'labels'         : label}

def build_dataloders(tokenizer,
                     train_dir = None,
                     val_dir   = None,
                     test_dir  = None,
                     max_len   = 128,
                     train_bs  = 8,
                     val_bs    = 8,
                     test_bs   = 1):
    # training dataset
    if train_dir:
        train_ds = MyDataset(train_dir, tokenizer, max_len)
        train_loader = DataLoader(train_ds, batch_size=train_bs, shuffle=True)
    else:
        train_loader = None
    
    # validation dataset
    if val_dir:
        val_ds = MyDataset(val_dir, tokenizer, max_len)
        val_loader = DataLoader(val_ds, batch_size=val_bs)
    else:
        val_loader = None
    
    if test_dir:
        # test dataset
        test_ds = MyDataset(test_dir, tokenizer, max_len)
        test_loader = DataLoader(test_ds, batch_size = test_bs)
    else:
        test_loader = None
    
    return train_loader, val_loader, test_loader

def compute_metrics(logits,
                    labels,
                    threshold = 0.5,
                    problem_type = "multi_label_classification"):
                        
    y_true = labels.cpu().numpy()
    if problem_type == "multi_label_classification":
        sigmoid = nn.Sigmoid()
        probs = sigmoid(torch.Tensor(logits))
        
        y_pred = np.zeros(probs.shape)
        y_pred[np.where(probs.cpu().detach().numpy() > threshold)] = 1
        
        f1 = f1_score(y_true, y_pred, average = 'weighted', zero_division=1)
        # roc_auc = roc_auc_score(y_true, y_pred)
        hamming = hamming_loss(y_true, y_pred)
        result = {"f1"        : f1,
                #  "roc_auc"   : roc_auc,
                 "hamming"   : hamming}



    else:
    
        y_pred = torch.argmax(logits, dim=-1)
        y_pred = y_pred.cpu().numpy()
    
        f1 = f1_score(y_true, y_pred, average = 'weighted')
        acc = accuracy_score(y_true, y_pred)
        result = {"f1"        : f1,
                 "accuracy"   : acc}


    return result

class Trainer:
    def __init__(self,
               dataset_dir,
               out_dir,
               num_classes,
               patience = None,
               max_len = 128,
               model_ckpt = "distilbert-base-uncased",
               problem_type = "multi_label_classification",
               optimizer = 'Adam',
               init_lr = 0.001,
               weight_decay = 0,
               scheduler_type = "linear",
               num_epochs = 100,
               train_bs = 8,
               val_bs = 8,
               device = 'cuda',
               clf_thrshold = 0.5):
    
        self.out_dir = out_dir
        self.patience = patience
        self.model_ckpt = model_ckpt
        self.num_epochs = num_epochs
        self.problem_type = problem_type
        self.device = device
        self.clf_thrshold = clf_thrshold
        
        # Tokenizer
        tokenizer = self._get_tokenizer_(model_ckpt = self.model_ckpt)
        
        # Model
        self.model = self._get_model_(model_ckpt   = self.model_ckpt,
                                      num_classes  = num_classes,
                                      problem_type = self.problem_type,
                                      device       = self.device)
        # Optimizer
        self.opt = self._get_optimizer(model        = self.model,
                                       name         = optimizer,
                                       init_lr      = init_lr,
                                       weight_decay = weight_decay)
        
        # Dataloader
        self.train_loader, self.val_loader, self.test_loader = build_dataloders(train_dir = os.path.join(dataset_dir, 'train'),
                                                                                val_dir   = os.path.join(dataset_dir, 'validation'),
                                                                                tokenizer = tokenizer,
                                                                                test_dir  = os.path.join(dataset_dir, 'test') if os.path.isdir(os.path.join(dataset_dir, 'test')) else None,
                                                                                max_len   = max_len,
                                                                                train_bs  = 8,
                                                                                val_bs    = 8)
        
        # Schedular
        num_training_steps = self.num_epochs * len(self.train_loader)
        self.lr_scheduler = get_scheduler(scheduler_type,
                                          optimizer          = self.opt,
                                          num_warmup_steps   = 0,
                                          num_training_steps = num_training_steps)
        
        # Make a dir to save results
        os.makedirs(os.path.join(out_dir, 'dump'), exist_ok=True)
        runs = [x for x in os.listdir(os.path.join(out_dir, 'dump')) if '.' not in x]
        if len(runs) == 0:
            self.out_dir = os.path.join(out_dir, 'dump/run_0')
            os.makedirs(os.path.join(out_dir, 'dump/run_0/evaluation'), exist_ok=True)
        else:
            idx = 0
            for name in runs:
                idx = max(idx, int(name.split('_')[-1]))
            self.out_dir    = os.path.join(out_dir, f'dump/run_{idx+1}')
            os.makedirs(os.path.join(out_dir, f'dump/run_{idx+1}/evaluation'), exist_ok=True)


    @staticmethod
    def _get_tokenizer_(model_ckpt):
        tokenizer = AutoTokenizer.from_pretrained(model_ckpt)
        return tokenizer
    
    
    @staticmethod
    def _get_model_(model_ckpt,
                  num_classes,
                  problem_type,
                  device):
                      
        config = AutoConfig.from_pretrained(model_ckpt,
                                            num_labels = num_classes,
                                            problem_type = problem_type)
        
        model  = AutoModelForSequenceClassification.from_pretrained(model_ckpt,
                                                                    config = config)
        
        model.to(device)
        return model

    @staticmethod
    def _get_optimizer(model, name, init_lr, weight_decay):
        if name == 'Adam':
          opt = torch.optim.Adam(model.parameters(),
                                  lr              = init_lr,
                                  betas           = (0.9, 0.999),
                                  eps             = 1e-08,
                                  weight_decay    = weight_decay,
                                  amsgrad         = False)
        
        elif name == 'Nadam':
          opt = torch.optim.NAdam(model.parameters(),
                                  lr             = init_lr,
                                  betas          = (0.9, 0.999),
                                  eps            = 1e-8,
                                  weight_decay   = weight_decay,
                                  momentum_decay = 0.001,
                                  foreach        = None)
        
        return opt


    def train_fn(self, epoch):
        loop = tqdm(self.train_loader)
        total_loss = 0
    
        for batch in self.train_loader:
            batch = {k: v.to(self.device) for k, v in batch.items()}
            
            # Forward
            outputs = self.model(**batch)
            loss = outputs.loss
            total_loss += loss.item()
    
            # Backward
            loss.backward()
            self.opt.step()
            self.lr_scheduler.step()
            self.opt.zero_grad()
    
            loop.update(1)
    
        return total_loss/len(self.train_loader)
    


    def _check_metrics(self,data):
        self.model.eval()
        all_logits = []
        all_labels = []
        
        for batch in data:
            batch = {k: v.to(self.device) for k, v in batch.items()}
            with torch.no_grad():
                outputs = self.model(**batch)
        
            logits = outputs.logits
        
            all_logits.append(logits)
            all_labels.append(batch["labels"])
        
        all_logits = torch.cat(all_logits, dim=0)
        all_labels = torch.cat(all_labels, dim=0)
        
        _metrics = compute_metrics(logits       = all_logits,
                                   labels       = all_labels,
                                   threshold    = self.clf_thrshold,
                                   problem_type = self.problem_type)
        
        return _metrics


    def _save_checkpoint(self, ckpt):
        print(f"=> Saving checkpoint with training loss = {ckpt['train_loss']}")
        torch.save(ckpt, os.path.join(self.out_dir, 'training_checkpoint.pth'))
        
            
    def train(self):
    
        max_score   = 0
        counter = 0
        
        for epoch in range(self.num_epochs):
        
            counter += 1
            
            print(f"On epoch {epoch+1}/{self.num_epochs}:")
            
            # Check performance on train data  using loss as a metric
            train_loss = self.train_fn(epoch)
            
            # Check performance on validation data using _check_metrics function
            val_metrics = self._check_metrics(self.val_loader)
            
        
    
            score = val_metrics['f1']
            if score > max_score:
                counter = 0
                max_score = score
                
                checkpoint = {"state_dict"   : self.model.state_dict(),
                              "optimizer"    : self.opt.state_dict(),
                              "train_loss"   : train_loss,
                              "val_metrics"  : val_metrics}
                              
                              
                if self.test_loader:
                    # Check performance on test data using _check_metrics function
                    test_metrics = self._check_metrics(self.test_loader)
                    checkpoint["test_metrics"] = test_metrics
    
    
                self._save_checkpoint(checkpoint)

            if self.patience and counter == self.patience: break

"""# Evaluation"""

class Evaluator:
    def __init__(self,
              dataset_dir,
              model_dir,
              num_classes,
              model_ckpt,
              problem_type,
              ckpt_name = 'training_checkpoint.pth',
              device = 'cuda',
              val_bs = 1,
              clf_thrshold = 0.5):
        self.model_dir    = model_dir
        self.problem_type = problem_type
        self.ckpt_name    = ckpt_name
        self.device       = device
        self.clf_thrshold = clf_thrshold
    
    
    
        # Tokenizer
        self.tokenizer = self._get_tokenizer_(model_ckpt)
        
        # Model
        self.model = self._load_model(self.model_dir,
                                      self.ckpt_name,
                                      num_classes,
                                      model_ckpt,
                                      self.problem_type,
                                      self.device)
        
        # Dataloader
        _, _, self.test_loader = build_dataloders(train_dir = None,
                                                  val_dir   = None,
                                                  tokenizer = self.tokenizer,
                                                  test_dir  = dataset_dir,
                                                  max_len   = 128,
                                                  test_bs   = 1)
        
    @staticmethod
    def _get_tokenizer_(model_ckpt):
        tokenizer = AutoTokenizer.from_pretrained(model_ckpt)
        return tokenizer
    
    
    @staticmethod
    def _load_model(model_dir,
                  ckpt_name,
                  num_classes,
                  model_ckpt,
                  problem_type,
                  device):
        ckpt = torch.load(os.path.join(model_dir, ckpt_name))
        
        
        config = AutoConfig.from_pretrained(model_ckpt,
                                            num_labels = num_classes,
                                            problem_type = problem_type)
        model  = AutoModelForSequenceClassification.from_pretrained(model_ckpt,
                                                                   config = config)
        
        model.to(device)
        model.load_state_dict(ckpt["state_dict"])
        model.eval()
        return model
    
    
    def evaluate(self):
    
        all_logits = []
        all_labels = []
        
        for batch in self.test_loader:
            batch = {k: v.to(self.device) for k, v in batch.items()}
            with torch.no_grad():
                outputs = self.model(**batch)
        
            logits = outputs.logits
        
            all_logits.append(logits)
            all_labels.append(batch["labels"])
        
        all_logits = torch.cat(all_logits, dim=0)
        all_labels = torch.cat(all_labels, dim=0)
        
        test_metrics = compute_metrics(logits       = all_logits,
                                       labels       = all_labels,
                                       threshold    = self.clf_thrshold,
                                       problem_type = self.problem_type)
        
        return test_metrics
        
        
    def prediction(self, text, multiLabel_binarizer=None):
        encoding = self.tokenizer(text, return_tensors='pt')
        encoding.to(self.device)
        with torch.no_grad():
            outputs = self.model(**encoding)
        sigmoid = nn.Sigmoid()
        probs = sigmoid(outputs.logits[0].cpu())
        preds = np.zeros(probs.shape)
        preds[np.where(probs >= self.clf_thrshold)] = 1
        if multiLabel_binarizer:
            pred_labels = multiLabel_binarizer.inverse_transform(preds.reshape(1,-1))
        else:
            pred_labels = preds.reshape(1,-1)
            

        return pred_labels
        
        
