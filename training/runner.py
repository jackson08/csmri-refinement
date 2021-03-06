import torch

import utils
from data.transform_wrappers import (get_output_transform,
                                     get_input_batch_transform)
from metrics import get_metric_fn, get_loss_metric
from models import construct_model
from models.criteria import get_criterion
from training.lr_schedulers import (get_lr_scheduler,
                                    is_pre_epoch_scheduler,
                                    is_post_epoch_scheduler)
from training.optimizers import get_optimizer
from training.base_runner import BaseRunner
from utils.checkpoints import initialize_pretrained_model
from utils.config import Configuration


def build_runner(conf, cuda, mode='train'):
  model_conf = Configuration.from_dict(conf.model, conf)

  model = construct_model(model_conf, model_conf.name, cuda)

  val_metric_fns = {name: get_metric_fn(conf, name, cuda, 'test')
                    for name in conf.get_attr('validation_metrics',
                                              default=[])}
  test_input_batch_transform = get_input_batch_transform(conf,
                                                         conf.application,
                                                         'test')
  output_transform = get_output_transform(conf, conf.application, 'inference')

  if mode == 'train':
    criteria = {}
    if conf.has_attr('loss_name'):
      criteria[conf.loss_name] = get_criterion(conf, conf.loss_name, cuda)
    else:
      for loss_name in conf.losses:
        criteria[loss_name] = get_criterion(conf, loss_name, cuda)

    assert len(criteria) > 0, 'Need at least one loss to optimize something!'

    if cuda != '':
      model = utils.cudaify(model, cuda)  # Potentially splits model over GPUs
      utils.cudaify(list(criteria.values()))

    # Important: construct optimizer after moving model to GPU!
    opt_conf = Configuration.from_dict(conf.optimizer, conf)
    optimizer = get_optimizer(opt_conf, opt_conf.name, model.parameters())

    lr_scheduler = None
    if opt_conf.has_attr('lr_scheduler'):
      lr_scheduler = get_lr_scheduler(opt_conf,
                                      opt_conf.lr_scheduler,
                                      optimizer)
    train_input_batch_transform = get_input_batch_transform(conf,
                                                            conf.application,
                                                            'train')
    train_metric_fns = {name: get_metric_fn(conf, name, cuda, 'train')
                        for name in conf.get_attr('train_metrics', default=[])}

    runner = Runner(model, criteria, conf.get_attr('loss_weights', {}),
                    optimizer, lr_scheduler, cuda,
                    train_metric_fns, val_metric_fns, output_transform,
                    train_input_batch_transform, test_input_batch_transform)

    if model_conf.has_attr('pretrained_weights'):
      initialize_pretrained_model(model_conf, runner.model, cuda, conf.file)
  else:
    if cuda != '':
      utils.cudaify(model)
    runner = Runner(model,
                    cuda=cuda,
                    val_metric_fns=val_metric_fns,
                    output_transform=output_transform,
                    test_input_batch_transform=test_input_batch_transform)

  return runner


class Runner(BaseRunner):
  """A runner for a simple single input, single output network"""
  def __init__(self, model, criteria={}, loss_weights={},
               optimizer=None, lr_scheduler=None, cuda='',
               train_metric_fns={}, val_metric_fns={},
               output_transform=None, train_input_batch_transform=None,
               test_input_batch_transform=None):
    super(Runner, self).__init__(cuda)
    self.model = model
    self.criteria = criteria
    self.loss_weights = self._get_loss_weights(loss_weights, criteria)
    self.optimizer = optimizer
    self.lr_scheduler = lr_scheduler
    self.train_metric_fns = train_metric_fns
    self.val_metric_fns = val_metric_fns
    self.output_transform = output_transform
    input_fn = self._get_model_input_fn(model, train_input_batch_transform)
    self.train_model_input_fn = input_fn
    input_fn = self._get_model_input_fn(model, test_input_batch_transform)
    self.test_model_input_fn = input_fn

  def get_named_outputs(self, data):
    batch, out_model = data[0], data[1]
    if isinstance(out_model, dict):
      prediction = out_model['pred']
    else:
      prediction = out_model

    target = batch['target']
    if self.output_transform is not None:
      prediction, target = self.output_transform(prediction, target)

    return {
        'input': batch['inp'],
        'prediction': prediction,
        'target': target
    }

  def get_named_models(self):
    return {
        'model': self.model
    }

  def state_dict(self):
    return {
        'model': self.model.state_dict(),
        'optimizer': self.optimizer.state_dict()
    }

  def load_state_dict(self, state_dict):
    self.model.load_state_dict(state_dict['model'])
    if self.optimizer is not None:
      assert 'optimizer' in state_dict, 'Incompatible checkpoint'
      self.optimizer.load_state_dict(state_dict['optimizer'])

  def __str__(self):
    s = 'Model:\n'
    s += str(self.model)
    return s

  def epoch_beginning(self, epoch):
    if is_pre_epoch_scheduler(self.lr_scheduler):
      self.lr_scheduler.step()

  def epoch_finished(self, epoch):
    if is_post_epoch_scheduler(self.lr_scheduler):
      self.lr_scheduler.step()

  def predict(self, batch):
    # Note: for now, we are not differentiating between train and validation
    # predict, so we are using train_model_input_fn without transform
    # for both cases.
    return self.model(*self.train_model_input_fn(batch,
                                                 use_batch_transform=False))

  def _train_step(self, loader):
    batch = self._request_data(loader)
    if batch is None:
      return 0, None, None

    self.optimizer.zero_grad()

    out_model = self.model(*self.train_model_input_fn(batch))

    losses = []
    loss_metrics = {}
    for name, criterion in self.criteria.items():
      loss = criterion(out_model, batch)
      losses.append(loss)
      loss_metrics['loss_' + name] = get_loss_metric(loss.data[0])

    total_loss = torch.sum(torch.cat(losses) * self.loss_weights)
    total_loss.backward()

    self.optimizer.step()

    loss_metrics['loss'] = get_loss_metric(total_loss.data[0])

    data = (batch, out_model)
    return 1, loss_metrics, data

  def _val_step(self, loader, compute_metrics=True):
    batch = self._request_data(loader, volatile=True)
    if batch is None:
      return None, None

    out_model = self.model(*self.test_model_input_fn(batch))

    loss_metrics = {}
    if compute_metrics:
      for name, criterion in self.criteria.items():
        loss = criterion(out_model, batch)
        loss_metrics['loss_' + name] = get_loss_metric(loss.data[0])

    return loss_metrics, (batch, out_model)

  def _compute_metrics(self, metric_fns, prediction, target):
    metrics = {}

    for metric_name, metric_fn in metric_fns.items():
      metric = metric_fn(prediction, target)
      metrics[metric_name] = metric

    return metrics

  def _compute_train_metrics(self, data):
    return self._compute_metrics(self.train_metric_fns,
                                 data[1], data[0])

  def _compute_test_metrics(self, data):
    return self._compute_metrics(self.val_metric_fns,
                                 data[1], data[0])

  def _set_train(self):
    self.model.train()

  def _set_test(self):
    self.model.eval()
